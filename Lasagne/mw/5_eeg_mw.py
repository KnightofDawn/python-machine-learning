from __future__ import print_function

import os
import time

import numpy as np
import theano
import theano.tensor as T
import lasagne
import matplotlib.pyplot as plt
import scipy.stats as stats

from lasagne.layers import InputLayer, Conv1DLayer, Pool1DLayer
from lasagne.regularization import regularize_network_params, l2

UPSAMPLE = True
VERBOSE = False

# Load EEG data
base_dir = os.path.abspath(os.path.join(os.path.join(os.path.dirname(__file__), os.pardir), os.pardir))
data_dir = os.path.join(base_dir, "data")

data = np.load(os.path.join(data_dir, 'all_data_6_2d_full.npy'))
data = data.reshape(-1, 1, 64, 512)

# Change to 64 channels of 1D vectors
data = np.transpose(data,(0, 2, 1, 3))  # Equivalent do tensor dimshuffle
data = data.squeeze()

# Standardize data per channel
data = stats.zscore(data, axis=2)  # Significantly improves gradient descent

# Get labels
data_labels = np.load(os.path.join(data_dir, 'all_data_6_2d_full_labels.npy'))
data_labels = data_labels[:,1]

# Upsample the under-represented MW class
if UPSAMPLE:
    mw_idx = np.where(data_labels==1)
    mw_data = data[mw_idx]
    mw_data_labels = data_labels[mw_idx]
    
    num_mw = len(mw_idx[0])
    num_ot = data.shape[0] - num_mw
    
    num_to_bootstrap = num_ot - num_mw
    bootstrap_idx = np.random.randint(mw_data.shape[0], size=num_to_bootstrap)
    mw_data_boot = mw_data[bootstrap_idx]
    mw_data_labels_boot = mw_data_labels[bootstrap_idx]
    
    data = np.concatenate((data, mw_data_boot), axis=0)
    data_labels = np.concatenate((data_labels, mw_data_labels_boot), axis=0)

# Create train, validation, test sets
indices = np.random.permutation(data.shape[0])

split_train, split_val, split_test = .6, .2, .2

split_train = int(round(data.shape[0]*split_train))
split_val = split_train + int(round(data.shape[0]*split_val))

train_idx = indices[:split_train]
val_idx = indices[split_train:split_val]
test_idx = indices[split_val:]

train_data = data[train_idx,:]
train_labels = data_labels[train_idx]

val_data = data[val_idx,:]
val_labels = data_labels[val_idx]

test_data = data[test_idx,:]
test_labels = data_labels[test_idx]

def build_cnn(input_var=None):
    # Input layer, as usual:
    l_in = InputLayer(shape=(None, 64, 512), input_var=input_var)

    l_conv1 = Conv1DLayer(incoming = l_in, num_filters = 64, filter_size = 3,
                        stride = 2, pad = 'same', W = lasagne.init.Normal(std = 0.02),
                        nonlinearity = lasagne.nonlinearities.rectify)
                        
    l_pool1 = Pool1DLayer(incoming = l_conv1, pool_size = 2, stride = 2)

    # A fully-connected layer
    l_fc = lasagne.layers.DenseLayer(
            lasagne.layers.dropout(l_pool1, p=.5),
            num_units=512,
            nonlinearity=lasagne.nonlinearities.rectify)

    l_out = lasagne.layers.DenseLayer(
            lasagne.layers.dropout(l_fc, p=.5),
            num_units=2,
            nonlinearity=lasagne.nonlinearities.softmax)

    return l_out
    
# ############################# Batch iterator ###############################
# This is just a simple helper function iterating over training data in
# mini-batches of a particular size, optionally in random order. It assumes
# data is available as numpy arrays. For big datasets, you could load numpy
# arrays as memory-mapped files (np.load(..., mmap_mode='r')), or write your
# own custom data iteration function. For small datasets, you can also copy
# them to GPU at once for slightly improved performance. This would involve
# several changes in the main program, though, and is not demonstrated here.
# Notice that this function returns only mini-batches of size `batchsize`.
# If the size of the data is not a multiple of `batchsize`, it will not
# return the last (remaining) mini-batch.

def iterate_minibatches(inputs, targets, batchsize, shuffle=False):
    assert len(inputs) == len(targets)
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)
    for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        yield inputs[excerpt], targets[excerpt]


# ############################## Main program ################################
# Everything else will be handled in our main program now. We could pull out
# more functions to better separate the code, but it wouldn't make it any
# easier to read.

def main(model='cnn', batch_size=500, num_epochs=500):
    # Prepare Theano variables for inputs and targets
    input_var = T.tensor3('inputs')
    target_var = T.ivector('targets')

    network = build_cnn(input_var)

    # Create a loss expression for training, i.e., a scalar objective we want
    # to minimize (for our multi-class problem, it is the cross-entropy loss):
    prediction = lasagne.layers.get_output(network)
    loss = lasagne.objectives.categorical_crossentropy(prediction, target_var)
    loss = loss.mean()
    
    # We could add some weight decay as well here, see lasagne.regularization.
    l2_reg = regularize_network_params(network, l2)
    loss += l2_reg * 0.001
    
    train_acc = T.mean(T.eq(T.argmax(prediction, axis=1), target_var),
                      dtype=theano.config.floatX)

    # Create update expressions for training, i.e., how to modify the
    # parameters at each training step. Here, we'll use Stochastic Gradient
    # Descent (SGD) with Nesterov momentum, but Lasagne offers plenty more.
    params = lasagne.layers.get_all_params(network, trainable=True)
    
    #updates = lasagne.updates.adam(loss, params, learning_rate=0.1)
    updates = lasagne.updates.nesterov_momentum(loss, params, learning_rate=0.01)

    # Create a loss expression for validation/testing. The crucial difference
    # here is that we do a deterministic forward pass through the network,
    # disabling dropout layers.
    test_prediction = lasagne.layers.get_output(network, deterministic=True)
    test_loss = lasagne.objectives.categorical_crossentropy(test_prediction,
                                                            target_var)
    test_loss = test_loss.mean()
    # As a bonus, also create an expression for the classification accuracy:
    test_acc = T.mean(T.eq(T.argmax(test_prediction, axis=1), target_var),
                      dtype=theano.config.floatX)

    # Compile a function performing a training step on a mini-batch (by giving
    # the updates dictionary) and returning the corresponding training loss:
    train_fn = theano.function([input_var, target_var], [loss, train_acc], updates=updates)

    # Compile a second function computing the validation loss and accuracy:
    val_fn = theano.function([input_var, target_var], [test_loss, test_acc])

    training_hist = []
    val_hist = []    
    
    # Finally, launch the training loop.
    print("Starting training...")
    # We iterate over epochs:
    for epoch in range(num_epochs):
        # In each epoch, we do a full pass over the training data:
        print("Training epoch {}...".format(epoch+1))
        train_err = 0
        train_acc = 0
        train_batches = 0
        start_time = time.time()
        for batch in iterate_minibatches(train_data, train_labels, batch_size, shuffle=True):
            inputs, targets = batch
            err, acc = train_fn(inputs, targets)
            train_err += err
            train_acc += acc
            train_batches += 1
            if VERBOSE:
                print("Epoch: {} | Mini-batch: {}/{} | Elapsed time: {:.2f}s".format(
                        epoch+1,
                        train_batches,
                        train_data.shape[0]/batch_size,
                        time.time()-start_time))

        training_hist.append(train_err / train_batches)

        # And a full pass over the validation data:
        print("Validating epoch...")
        val_err = 0
        val_acc = 0
        val_batches = 0
        for batch in iterate_minibatches(val_data, val_labels, batch_size, shuffle=False):
            inputs, targets = batch
            err, acc = val_fn(inputs, targets)
            val_err += err
            val_acc += acc
            val_batches += 1
            
        val_hist.append(val_err / val_batches)

        # Then we print the results for this epoch:
        print("Epoch {} of {} took {:.3f}s".format(
            epoch + 1, num_epochs, time.time() - start_time))
        print("  training loss:\t\t{:.6f}".format(train_err / train_batches))
        print("  training accuracy:\t\t{:.2f} %".format(
            train_acc / train_batches * 100))
        print("  validation loss:\t\t{:.6f}".format(val_err / val_batches))
        print("  validation accuracy:\t\t{:.2f} %".format(
            val_acc / val_batches * 100))

    # After training, we compute and print the test predictions/error:
    test_err = 0
    test_acc = 0
    test_batches = 0
    for batch in iterate_minibatches(test_data, test_labels, batch_size, shuffle=False):
        inputs, targets = batch
        err, acc = val_fn(inputs, targets)
        test_err += err
        test_acc += acc
        test_batches += 1
    print("Final results:")
    print("  test loss:\t\t\t{:.6f}".format(test_err / test_batches))
    print("  test accuracy:\t\t{:.2f} %".format(
        test_acc / test_batches * 100))
        
    # Plot learning
    plt.plot(range(1, num_epochs+1), training_hist, label="Training")
    plt.plot(range(1, num_epochs+1), val_hist, label="Validation")
    plt.grid(True)
    plt.title("Training Curve")
    plt.xlim(1, num_epochs+1)
    plt.xlabel("Epoch #")
    plt.ylabel("Loss")
    plt.legend(loc='best')
    plt.show()

    # Optionally, you could now dump the network weights to a file like this:
    # np.savez('model.npz', *lasagne.layers.get_all_param_values(network))
    #
    # And load them again later on like this:
    # with np.load('model.npz') as f:
    #     param_values = [f['arr_%d' % i] for i in range(len(f.files))]
    # lasagne.layers.set_all_param_values(network, param_values)

# Run the model
main(batch_size=100, num_epochs=100)