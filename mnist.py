import tensorflow as tf
import numpy as np
import pandas as pd
from datetime import datetime

from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.exceptions import NotFittedError
from sklearn.metrics import accuracy_score
from sklearn.model_selection import RandomizedSearchCV, GridSearchCV, cross_val_score

from functools import partial

he_init = tf.variance_scaling_initializer()

class DNNClassifier(BaseEstimator, ClassifierMixin):
    def __init__(self, n_hidden_layers=5, n_neurons=100, optimizer_class=tf.train.AdamOptimizer,
                 learning_rate=0.001, batch_size=20, activation=tf.nn.elu, initializer=he_init,
                 batch_norm_momentum=None, dropout_rate=None, random_state=None, checks=20,
                 X_valid=None, y_valid=None, max_iter=20000):
        """Initialize the DNNClassifier by simply storing all the hyperparameters."""
        self.n_hidden_layers = n_hidden_layers
        self.n_neurons = n_neurons
        self.optimizer_class = optimizer_class
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.activation = activation
        self.initializer = initializer
        self.batch_norm_momentum = batch_norm_momentum
        self.dropout_rate = dropout_rate
        self.random_state = random_state
        self._session = None
        self.checks = checks

        self.X_valid = X_valid
        self.y_valid = y_valid
        self.max_iter = max_iter

    def _dnn(self, inputs):
        """Build the hidden layers, with support for batch normalization and dropout."""
        for layer in range(self.n_hidden_layers):
            if self.dropout_rate:
                inputs = tf.layers.dropout(inputs, self.dropout_rate, training=self._training)
            inputs = tf.layers.dense(inputs, self.n_neurons,
                                     kernel_initializer=self.initializer,
                                     name="hidden%d" % (layer + 1))
            if self.batch_norm_momentum:
                inputs = tf.layers.batch_normalization(inputs, momentum=self.batch_norm_momentum,
                                                       training=self._training)
            inputs = self.activation(inputs, name="hidden%d_out" % (layer + 1))
        return inputs

    def _build_graph(self, n_inputs, n_outputs):
        """Build the same model as earlier"""
        if self.random_state is not None:
            tf.set_random_seed(self.random_state)
            np.random.seed(self.random_state)

        X = tf.placeholder(tf.float32, shape=(None, n_inputs), name="X")
        y = tf.placeholder(tf.int32, shape=(None), name="y")

        if self.batch_norm_momentum or self.dropout_rate:
            self._training = tf.placeholder_with_default(False, shape=(), name='training')
        else:
            self._training = None

        dnn_outputs = self._dnn(X)

        logits = tf.layers.dense(dnn_outputs, n_outputs, kernel_initializer=he_init, name="logits")
        Y_proba = tf.nn.softmax(logits, name="Y_proba")

        xentropy = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=y,
                                                                  logits=logits)
        loss = tf.reduce_mean(xentropy, name="loss")

        optimizer = self.optimizer_class(learning_rate=self.learning_rate)
        training_op = optimizer.minimize(loss)

        correct = tf.nn.in_top_k(logits, y, 1)
        accuracy = tf.reduce_mean(tf.cast(correct, tf.float32), name="accuracy")

        init = tf.global_variables_initializer()
        saver = tf.train.Saver()

        # Make the important operations available easily through instance variables
        self._X, self._y = X, y
        self._Y_proba, self._loss = Y_proba, loss
        self._training_op, self._accuracy = training_op, accuracy
        self._init, self._saver = init, saver

    def close_session(self):
        if self._session:
            self._session.close()

    def _get_model_params(self):
        """Get all variable values (used for early stopping, faster than saving to disk)"""
        with self._graph.as_default():
            gvars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
        return {gvar.op.name: value for gvar, value in zip(gvars, self._session.run(gvars))}

    def _restore_model_params(self, model_params):
        """Set all variables to the given values (for early stopping, faster than loading from disk)"""
        gvar_names = list(model_params.keys())
        assign_ops = {gvar_name: self._graph.get_operation_by_name(gvar_name + "/Assign")
                      for gvar_name in gvar_names}
        init_values = {gvar_name: assign_op.inputs[1] for gvar_name, assign_op in assign_ops.items()}
        feed_dict = {init_values[gvar_name]: model_params[gvar_name] for gvar_name in gvar_names}
        self._session.run(assign_ops, feed_dict=feed_dict)

    def fit(self, X, y, n_epochs=100, X_valid=None, y_valid=None):
        """Fit the model to the training set. If X_valid and y_valid are provided, use early stopping."""
        self.close_session()

        X_valid = X_valid or self.X_valid
        y_valid = y_valid or self.y_valid

        # infer n_inputs and n_outputs from the training set.
        n_inputs = X.shape[1]
        self.classes_ = np.unique(y)
        n_outputs = len(self.classes_)
        
        # Translate the labels vector to a vector of sorted class indices, containing
        # integers from 0 to n_outputs - 1.
        # For example, if y is equal to [8, 8, 9, 5, 7, 6, 6, 6], then the sorted class
        # labels (self.classes_) will be equal to [5, 6, 7, 8, 9], and the labels vector
        # will be translated to [3, 3, 4, 0, 2, 1, 1, 1]
        self.class_to_index_ = {label: index
                                for index, label in enumerate(self.classes_)}
        y = np.array([self.class_to_index_[label]
                      for label in y], dtype=np.int32)
        
        self._graph = tf.Graph()
        with self._graph.as_default():
            self._build_graph(n_inputs, n_outputs)
            # extra ops for batch normalization
            extra_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

        # needed in case of early stopping
        max_checks_without_progress = self.checks
        checks_without_progress = 0
        best_loss = np.infty
        best_params = None

        # run for fixed number of iterations
        num_runs = 0
        self.val_acc = []

        # Now train the model!
        self._session = tf.Session(graph=self._graph)
        with self._session.as_default() as sess:
            self._init.run()
            # for epoch in range(n_epochs):
            while True:
                epoch = 0
                rnd_idx = np.random.permutation(len(X))
                for rnd_indices in np.array_split(rnd_idx, len(X) // self.batch_size):

                    num_runs += self.batch_size
                    if num_runs > self.max_iter:
                        print("Early stopping!", self.max_iter, "samples trained on.")
                        self.save_val_plot()
                        return

                    X_batch, y_batch = X[rnd_indices], y[rnd_indices]
                    feed_dict = {self._X: X_batch, self._y: y_batch}
                    if self._training is not None:
                        feed_dict[self._training] = True
                    sess.run(self._training_op, feed_dict=feed_dict)
                    if extra_update_ops:
                        sess.run(extra_update_ops, feed_dict=feed_dict)
                if X_valid is not None and y_valid is not None:
                    loss_val, acc_val = sess.run([self._loss, self._accuracy],
                                                 feed_dict={self._X: X_valid,
                                                            self._y: y_valid})
                    self.val_acc.append({"Epoch": num_runs, "Validation": acc_val})
                    if loss_val < best_loss:
                        best_params = self._get_model_params()
                        best_loss = loss_val
                        checks_without_progress = 0
                    else:
                        checks_without_progress += 1
                    print("{}\tValidation loss: {:.6f}\tBest loss: {:.6f}\tAccuracy: {:.2f}%".format(
                        epoch, loss_val, best_loss, acc_val * 100))
                    # if checks_without_progress > max_checks_without_progress:
                    #     print("Early stopping!")
                    #     break
                else:
                    loss_train, acc_train = sess.run([self._loss, self._accuracy],
                                                     feed_dict={self._X: X_batch,
                                                                self._y: y_batch})
                    print("{}\tLast training batch loss: {:.6f}\tAccuracy: {:.2f}%".format(
                        epoch, loss_train, acc_train * 100))
            # If we used early stopping then rollback to the best model found
            if best_params:
                self._restore_model_params(best_params)

            self.save_val_plot()
            return self

    def save_val_plot(self):
        df = pd.DataFrame(self.val_acc, columns=["Epoch", "Validation"])
        now = datetime.now().strftime("%H-%M-%S")
        fn = "./val_scores/" + now + ".csv"
        df.to_csv(fn, index=False)

    def predict_proba(self, X):
        if not self._session:
            raise NotFittedError("This %s instance is not fitted yet" % self.__class__.__name__)
        with self._session.as_default() as sess:
            return self._Y_proba.eval(feed_dict={self._X: X})

    def predict(self, X):
        class_indices = np.argmax(self.predict_proba(X), axis=1)
        return np.array([[self.classes_[class_index]]
                         for class_index in class_indices], np.int32)

    def save(self, path):
        self._saver.save(self._session, path)

def main():
  X = np.load("x_mnist1000.npy")
  y = np.load("y_mnist1000.npy")
  
  M = len(X)
  K = 10
  
  Y = np.zeros((M, K))
  Y[(range(M), y)] = 1
  
  # set seed to 1
  np.random.seed(1)
  
  # split data
  indices = np.random.permutation(M)
  train_indices = indices[:800]
  val_indices = indices[800:900]
  test_indices = indices[900:]
  
  X_train = X[train_indices]
  Y_train = Y[train_indices]
  y_train = y[train_indices]

  X_valid = X[val_indices]
  y_valid = y[val_indices]
  
  X_test = X[test_indices]
  Y_test = Y[test_indices]
  y_test = y[test_indices]
  
  # compute mean, std per column
  X_train_mean = X_train.mean(axis=0)
  X_train_std = X_train.std(axis=0)
  
  # replace std of 0 with 1
  X_train_std[X_train_std == 0] = 1
  
  # scale train and test data
  X_train = (X_train - X_train_mean) / X_train_std
  X_test = (X_test - X_train_mean) / X_train_std
  X_valid = (X_valid - X_train_mean) / X_train_std

  def leaky_relu(alpha=0.01):
    def parametrized_leaky_relu(z, name=None):
      return tf.maximum(alpha * z, z, name=name)
    return parametrized_leaky_relu

  dnn_clf = DNNClassifier(random_state=1)

  param_distribs = {
    # "n_neurons": [10, 30, 50, 70, 90, 100, 120, 140, 160],
    # "batch_size": [10, 50, 100, 500],
    # "learning_rate": [0.01, 0.02, 0.05, 0.1],
    # "activation": [tf.nn.relu, tf.nn.tanh, tf.nn.elu, tf.nn.selu],
    "activation": [tf.nn.selu],
    # you could also try exploring different numbers of hidden layers, different optimizers, etc.
    "n_hidden_layers": [1, 2, 3],
    # "optimizer_class": [tf.train.AdamOptimizer, partial(tf.train.MomentumOptimizer, momentum=0.95)],
    # "dropout_rate": [0, 0.1, 0.2],
    "X_valid": [X_valid],
    "y_valid": [y_valid],
    "max_iter": [40000],
  }

  gs = GridSearchCV(dnn_clf, param_distribs, n_jobs=-1)
  gs.fit(X_train, y_train)

  clf = gs.best_estimator_
  print("Best score:", gs.best_score_)
  print("Params:", gs.best_params_)

  y_pred_train = clf.predict(X_train)
  cv_scores = cross_val_score(clf, X_train, y_train, cv=5)
  y_pred = clf.predict(X_test)

  print("Training accuracy:", accuracy_score(y_train, y_pred_train))
  print("Cross val scores:", cv_scores)
  print("Mean:", np.mean(cv_scores))
  print("std:", np.std(cv_scores))
  print("Test accuracy:", accuracy_score(y_test, y_pred))

  df = pd.DataFrame(gs.cv_results_)
  now = datetime.now().strftime("%H-%M-%S")
  df.to_csv("./gs_results/" + now + ".csv", index=False)

main()
 
 
