
import numpy as np
from tensorflow.keras.layers import concatenate, Lambda, GaussianNoise, Dropout, GaussianDropout, AlphaDropout, Activation, LeakyReLU
import tensorflow.keras.backend as K
from tensorflow.keras.initializers import glorot_normal, glorot_uniform, he_normal, lecun_normal
from tensorflow.keras.layers import Input, Dense, BatchNormalization, Layer, InputSpec, Activation
from tensorflow.keras.models import Model
from sklearn.cluster import KMeans, MiniBatchKMeans

def reproducibility(seed_value=1, cpu=True):
    import os
    if cpu:
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"   # see issue #152
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    os.environ['PYTHONHASHSEED']=str(seed_value)
    os.environ['TF_DETERMINISTIC_OPS'] = '1'
    os.environ['TF_CUDNN_DETERMINISTIC'] = '1'
    # from tfdeterminism import patch
    # patch()
    import random
    random.seed(seed_value)
    import numpy as np
    np.random.seed(seed_value)
    from tensorflow import random as tf_rd
    tf_rd.set_seed(seed_value)

# seed_value = 1
# reproducibility(seed_value)
# init = glorot_uniform(seed=seed_value)

# @staticmethod
def target_distribution(q):  # target distribution P which enhances the discrimination of soft label Q
    weight = q ** 2 / q.sum(0)
    return (weight.T / weight.sum(1)).T

def target_distribution_u(q):  # target distribution P which enhances the discrimination of soft label Q
    n_cluster = q.shape[1]
    # np.set_seed(3)
    u = np.sort(np.random.exponential(1, n_cluster))
    u = u/np.sum(u)
    weight = (u*q) ** 2 / (u*q).sum(0)
    return (weight.T / weight.sum(1)).T

# coefficient of determination (R^2) for regression  (only for Keras tensors)
def r_square(y_true, y_pred):
    SS_res =  K.sum(K.square(y_true - y_pred)) 
    SS_tot = K.sum(K.square(y_true - K.mean(y_true))) 
    return ( 1 - SS_res/(SS_tot + K.epsilon()) )

class ClusteringLayer(Layer):
    def __init__(self, n_clusters, weights=None, alpha=1.0, **kwargs):
        if 'input_shape' not in kwargs and 'input_dim' in kwargs:
            kwargs['input_shape'] = (kwargs.pop('input_dim'),)
        super(ClusteringLayer, self).__init__(**kwargs)
        self.n_clusters = n_clusters
        self.alpha = alpha
        self.initial_weights = weights
        self.input_spec = InputSpec(ndim=2)

    def build(self, input_shape):
        assert len(input_shape) == 2
        input_dim = input_shape[1]
        self.input_spec = InputSpec(dtype=K.floatx(), shape=(None, input_dim))
        self.clusters = self.add_weight(shape=(self.n_clusters, input_dim), initializer=glorot_uniform(seed=1), name='clusters')
        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights
        self.built = True

    def call(self, inputs, **kwargs):
        """ student t-distribution, as same as used in t-SNE algorithm.
                 q_ij = 1/(1+dist(x_i, u_j)^2), then normalize it.
        Arguments:
            inputs: the variable containing data, shape=(n_samples, n_features)
        Return:
            q: student's t-distribution, or soft labels for each sample. shape=(n_samples, n_clusters)
        """
        q = 1.0 / (1.0 + (K.sum(K.square(K.expand_dims(inputs, axis=1) - self.clusters), axis=2) / self.alpha))
        q **= (self.alpha + 1.0) / 2.0
        q = K.transpose(K.transpose(q) / K.sum(q, axis=1))
        return q

    def compute_output_shape(self, input_shape):
        assert input_shape and len(input_shape) == 2
        return input_shape[0], self.n_clusters

    def get_config(self):
        config = {'n_clusters': self.n_clusters}
        base_config = super(ClusteringLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

def clustering3(model, encoder, x, y=None,
                   tol=1e-3,
                   update_interval=140,
                   maxiter=2e4,
                   batch_size=256,
                   n_clusters=15,
                   save_dir='./results/idec'):
        print('Update interval', update_interval)
        save_interval = x.shape[0] / batch_size * 5  # 5 epochs
        print('Save interval', save_interval)
        # initialize cluster centers using k-means
        print('Initializing cluster centers with k-means.')
        kmeans = KMeans(n_clusters=n_clusters, random_state=None, n_init=25)
        y_pred = kmeans.fit_predict(encoder.predict([x, x, x]))
        y_pred_last = y_pred
        model.get_layer(name='clustering').set_weights([kmeans.cluster_centers_])
        loss = [0, 0, 0, 0, 0]
        index = 0
        for ite in range(int(maxiter)):
            if ite % update_interval == 0:
                q, _, _, _ = model.predict([x, x, x], verbose=0)
                p = target_distribution(q)  # update the auxiliary target distribution p
                # evaluate the clustering performance
                y_pred = q.argmax(1)
                delta_label = np.sum(y_pred != y_pred_last).astype(np.float32) / y_pred.shape[0]
                y_pred_last = y_pred
                print('At ite {}, there are {} clusters, loss is {}, and delta is {:.4f}'.format(
                    ite, len(np.unique(y_pred)), ['{:.2f}'.format(l) for l in loss], delta_label))
                # check stop criterion
                if ite > 0 and delta_label < tol:
                    print('delta_label ', delta_label, '< tol ', tol)
                    print('Reached tolerance threshold. Stopping training.')
                    # logfile.close()
                    break
            # train on batch
            if (index + 1) * batch_size > x.shape[0]:
                loss = model.train_on_batch(x=[x[index * batch_size::], x[index * batch_size::], x[index * batch_size::]],
                                                 y=[p[index * batch_size::], x[index * batch_size::], 
                                                    x[index * batch_size::], x[index * batch_size::]])
                index = 0
            else:
                loss = model.train_on_batch(x=[x[index * batch_size:(index + 1) * batch_size], 
                                               x[index * batch_size:(index + 1) * batch_size],
                                               x[index * batch_size:(index + 1) * batch_size]],
                                                 y=[p[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size]])
                index += 1
            ite += 1
        return y_pred


def autoencoder_(dims, act='relu', uniqueID = '0', l2=False, init='glorot_uniform', noise=False, dropout=False):
        n_stacks = len(dims) - 1
        # input
        # act = LeakyReLU(alpha=0.3)
        x = Input(shape=(dims[0],), name='input' + uniqueID)
        h = x
        # internal layers in encoder
        for i in range(n_stacks-1):
            h = Dense(units=dims[i + 1], activation=act, kernel_initializer=init, name='encoder_' + uniqueID +'%d' % i)(h) #, use_bias=False
            if noise:
                h = GaussianNoise(stddev=1, name='noise_' + uniqueID +'%d' % i)(h)
        # hidden layer
        h = Dense(units=dims[-1], kernel_initializer=init, name='encoder_' + uniqueID +'%d' % (n_stacks - 1))(h) #, use_bias=False
        if l2:
            h = Lambda(lambda  x: K.l2_normalize(x, axis=1), name='encoder_a' + uniqueID +'%d' % (n_stacks - 1))(h)
            # h = BatchNormalization(name='encoder_a' + uniqueID +'%d' % (n_stacks - 1))(h)
        # internal layers in decoder
        for i in range(n_stacks-1, 0, -1):
            h = Dense(units=dims[i], activation=act, kernel_initializer=init, name='decoder_' + uniqueID +'%d' % i)(h) #, use_bias=False
            if dropout:
                h = GaussianDropout(0.3, name='dropout_' + uniqueID +'%d' % i)(h)
        # output
        h = Dense(units=dims[0], kernel_initializer=init, name='decoder_' + uniqueID +'0')(h) #use_bias=False, 
        return Model(inputs=x, outputs=h)

def clustering2(model, encoder, x, y=None,
                   tol=1e-3,
                   k_seed=None,
                   update_interval=140,
                   maxiter=2e4,
                   batch_size=256,
                   n_clusters=15,
                   save_dir='./results/idec'):
        print('Update interval', update_interval)
        save_interval = x.shape[0] / batch_size * 5  # 5 epochs
        print('Save interval', save_interval)
        # initialize cluster centers using k-means
        print('Initializing cluster centers with k-means.')
        kmeans = MiniBatchKMeans(n_clusters=n_clusters, random_state=k_seed)#, n_init=25)
        y_pred = kmeans.fit_predict(encoder.predict([x, x]))
        y_pred_last = y_pred
        model.get_layer(name='clustering').set_weights([kmeans.cluster_centers_])
        loss = [0, 0, 0, 0]
        index = 0
        for ite in range(int(maxiter)):
            if ite % update_interval == 0:
                q, _, _ = model.predict([x, x], verbose=0)
                p = target_distribution(q)  # update the auxiliary target distribution p
                # evaluate the clustering performance
                y_pred = q.argmax(1)
                delta_label = np.sum(y_pred != y_pred_last).astype(np.float32) / y_pred.shape[0]
                y_pred_last = y_pred
                print('At ite {}, there are {} clusters, loss is {}, and delta is {:.4f}'.format(
                    ite, len(np.unique(y_pred)), ['{:.2f}'.format(l) for l in loss], delta_label))
                # check stop criterion
                if ite > 0 and delta_label < tol:
                    print('delta_label ', delta_label, '< tol ', tol)
                    print('Reached tolerance threshold. Stopping training.')
                    # logfile.close()
                    break
            # train on batch
            if (index + 1) * batch_size > x.shape[0]:
                loss = model.train_on_batch(x=[x[index * batch_size::], x[index * batch_size::]],
                                                 y=[p[index * batch_size::], x[index * batch_size::], 
                                                    x[index * batch_size::]])
                index = 0
            else:
                loss = model.train_on_batch(x=[x[index * batch_size:(index + 1) * batch_size], 
                                               x[index * batch_size:(index + 1) * batch_size]],
                                                 y=[p[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size]])
                index += 1
            ite += 1
        return y_pred

def clustering2K(model, encoder, x, y=None,
                   tol=1e-3,
                   k_seed=None,
                   update_interval=140,
                   maxiter=2e4,
                   batch_size=256,
                   n_clusters=15,
                   save_dir='./results/idec'):
        print('Update interval', update_interval)
        save_interval = x.shape[0] / batch_size * 5  # 5 epochs
        print('Save interval', save_interval)
        # initialize cluster centers using k-means
        print('Initializing cluster centers with k-means.')
        kmeans = KMeans(n_clusters=n_clusters, random_state=k_seed, n_init=5)
        y_pred = kmeans.fit_predict(encoder.predict([x, x]))
        y_pred_last = y_pred
        model.get_layer(name='clustering').set_weights([kmeans.cluster_centers_])
        loss = [0, 0, 0, 0]
        index = 0
        for ite in range(int(maxiter)):
            if ite % update_interval == 0:
                q, _, _ = model.predict([x, x], verbose=0)
                p = target_distribution(q)  # update the auxiliary target distribution p
                # evaluate the clustering performance
                y_pred = q.argmax(1)
                delta_label = np.sum(y_pred != y_pred_last).astype(np.float32) / y_pred.shape[0]
                y_pred_last = y_pred
                print('At ite {}, there are {} clusters, loss is {}, and delta is {:.4f}'.format(
                    ite, len(np.unique(y_pred)), ['{:.2f}'.format(l) for l in loss], delta_label))
                # check stop criterion
                if ite > 0 and delta_label < tol:
                    print('delta_label ', delta_label, '< tol ', tol)
                    print('Reached tolerance threshold. Stopping training.')
                    # logfile.close()
                    break
            # train on batch
            if (index + 1) * batch_size > x.shape[0]:
                loss = model.train_on_batch(x=[x[index * batch_size::], x[index * batch_size::]],
                                                 y=[p[index * batch_size::], x[index * batch_size::], 
                                                    x[index * batch_size::]])
                index = 0
            else:
                loss = model.train_on_batch(x=[x[index * batch_size:(index + 1) * batch_size], 
                                               x[index * batch_size:(index + 1) * batch_size]],
                                                 y=[p[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size]])
                index += 1
            ite += 1
        return y_pred



# kmeans = KMeans(n_clusters=n_clusters, random_state=None, n_init=25)
# y_pred_10 = kmeans.fit_predict(encoder_10.predict(x))


# from sklearn.metrics import normalized_mutual_info_score as NMI
# encoder = Model(inputs=[ae1.input, ae3.input], outputs=merged_hidden)
# kmeans = KMeans(n_clusters=n_clusters, random_state=None, n_init=5)
# y_pred_2 = kmeans.fit_predict(encoder.predict([x, x]))
# NMI(y_pred_0, y_pred_1)
# NMI(y_pred_2, y_pred_1)
# NMI(y_pred_0, y_pred_2)


# NMI(y_pred_11, y_pred_10)
# NMI(y_pred_11, y_pred_12)
# NMI(y_pred_12, y_pred_10)
# NMI(y_pred_31, y_pred_30)
# NMI(y_pred_31, y_pred_32)
# NMI(y_pred_32, y_pred_30)

# NMI(y_pred_30, y_pred_10)
# NMI(y_pred_31, y_pred_11)
# NMI(y_pred_32, y_pred_12)

# kmeans = KMeans(n_clusters=n_clusters, random_state=k_seed, n_init=25)
# y_pred_0 = kmeans.fit_predict(np.hstack((encoder_10.predict(x), encoder_30.predict(x))))

# np.hstack((encoder_10.predict(x), encoder_30.predict(x))).shape
# encoder_10.predict(x).shape
# encoder_30.predict(x).shape

def clustering2K_std(model, encoder, x1, x2, y=None,
                   tol=1e-3,
                   k_seed=None,
                   update_interval=140,
                   maxiter=2e4,
                   batch_size=256,
                   n_clusters=15,
                   save_dir='./results/idec'):
        print('Update interval', update_interval)
        save_interval = x1.shape[0] / batch_size * 5  # 5 epochs
        print('Save interval', save_interval)
        # initialize cluster centers using k-means
        print('Initializing cluster centers with k-means.')
        kmeans = KMeans(n_clusters=n_clusters, random_state=k_seed, n_init=25)
        y_pred = kmeans.fit_predict(encoder.predict([x1, x2]))
        y_pred_last = y_pred
        model.get_layer(name='clustering').set_weights([kmeans.cluster_centers_])
        loss = [0, 0, 0, 0]
        index = 0
        for ite in range(int(maxiter)):
            if ite % update_interval == 0:
                q, _, _ = model.predict([x1, x2], verbose=0)
                p = target_distribution(q)  # update the auxiliary target distribution p
                # evaluate the clustering performance
                y_pred = q.argmax(1)
                delta_label = np.sum(y_pred != y_pred_last).astype(np.float32) / y_pred.shape[0]
                y_pred_last = y_pred
                print('At ite {}, there are {} clusters, loss is {}, and delta is {:.4f}'.format(
                    ite, len(np.unique(y_pred)), ['{:.2f}'.format(l) for l in loss], delta_label))
                # check stop criterion
                if ite > 0 and delta_label < tol:
                    print('delta_label ', delta_label, '< tol ', tol)
                    print('Reached tolerance threshold. Stopping training.')
                    # logfile.close()
                    break
            # train on batch
            if (index + 1) * batch_size > x1.shape[0]:
                loss = model.train_on_batch(x=[x1[index * batch_size::], x2[index * batch_size::]],
                                                 y=[p[index * batch_size::], x1[index * batch_size::], 
                                                    x2[index * batch_size::]])
                index = 0
            else:
                loss = model.train_on_batch(x=[x1[index * batch_size:(index + 1) * batch_size], 
                                               x2[index * batch_size:(index + 1) * batch_size]],
                                                 y=[p[index * batch_size:(index + 1) * batch_size],
                                                    x1[index * batch_size:(index + 1) * batch_size],
                                                    x2[index * batch_size:(index + 1) * batch_size]])
                index += 1
            ite += 1
        return y_pred

def clustering(model, encoder, x, y=None,
                   tol=1e-3,
                   k_seed=None,
                   update_interval=140*3,
                   maxiter=2e6,
                   batch_size=256,
                   n_clusters=15,
                   save_dir='./results/idec'):
        print('Update interval', update_interval)
        save_interval = x.shape[0] / batch_size * 5  # 5 epochs
        print('Save interval', save_interval)
        # initialize cluster centers using k-means
        print('Initializing cluster centers with k-means.')
        kmeans = KMeans(n_clusters=n_clusters, random_state=k_seed, n_init=25)
        y_pred = kmeans.fit_predict(encoder.predict(x))
        y_pred_last = y_pred
        model.get_layer(name='clustering').set_weights([kmeans.cluster_centers_])
        loss = [0, 0, 0] #total averaged loss, clustering loss, and reconstruction losss 
        index = 0
        for ite in range(int(maxiter)):
            if ite % update_interval == 0:
                q, _ = model.predict(x, verbose=0)
                p = target_distribution(q)  # update the auxiliary target distribution p
                # evaluate the clustering performance
                y_pred = q.argmax(1)
                delta_label = np.sum(y_pred != y_pred_last).astype(np.float32) / y_pred.shape[0]
                y_pred_last = y_pred
                print('At ite {}, there are {} clusters, loss is {}, and delta is {:.4f}'.format(
                    ite, len(np.unique(y_pred)), ['{:.2f}'.format(l) for l in loss], delta_label))
                # check stop criterion
                if ite > 0 and delta_label < tol:
                    print('delta_label ', delta_label, '< tol ', tol)
                    print('Reached tolerance threshold. Stopping training.')
                    # logfile.close()
                    break
            # train on batch
            if (index + 1) * batch_size > x.shape[0]:
                loss = model.train_on_batch(x=x[index * batch_size::],
                                                 y=[p[index * batch_size::], x[index * batch_size::]])
                index = 0
            else:
                loss = model.train_on_batch(x=x[index * batch_size:(index + 1) * batch_size],
                                                 y=[p[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size]])
                index += 1
            ite += 1
        return y_pred

def clustering3_u(model, encoder, x, y=None,
                   tol=1e-3,
                   update_interval=140,
                   maxiter=2e4,
                   batch_size=256,
                   n_clusters=15,
                   save_dir='./results/idec'):
        print('Update interval', update_interval)
        save_interval = x.shape[0] / batch_size * 5  # 5 epochs
        print('Save interval', save_interval)
        # initialize cluster centers using k-means
        print('Initializing cluster centers with k-means.')
        kmeans = MiniBatchKMeans(n_clusters=n_clusters, random_state=1)#, n_init=20)
        y_pred = kmeans.fit_predict(encoder.predict([x, x, x]))
        y_pred_last = y_pred
        _, count = np.unique(y_pred, return_counts=True)
        cl_sizeIndex = np.flip(np.argsort(count))
        model.get_layer(name='clustering').set_weights([kmeans.cluster_centers_[cl_sizeIndex, :]])
        loss = [0, 0, 0, 0, 0]
        index = 0
        for ite in range(int(maxiter)):
            if ite % update_interval == 0:
                q, _, _, _ = model.predict([x, x, x], verbose=0)
                p = target_distribution_u(q)  # update the auxiliary target distribution p
                # evaluate the clustering performance
                y_pred = q.argmax(1)
                delta_label = np.sum(y_pred != y_pred_last).astype(np.float32) / y_pred.shape[0]
                y_pred_last = y_pred
                print('At ite {}, there are {} clusters, loss is {}, and delta is {:.4f}'.format(
                    ite, len(np.unique(y_pred)), ['{:.2f}'.format(l) for l in loss], delta_label))
                # check stop criterion
                if ite > 0 and delta_label < tol:
                    print('delta_label ', delta_label, '< tol ', tol)
                    print('Reached tolerance threshold. Stopping training.')
                    # logfile.close()
                    break
            # train on batch
            if (index + 1) * batch_size > x.shape[0]:
                loss = model.train_on_batch(x=[x[index * batch_size::], x[index * batch_size::], x[index * batch_size::]],
                                                 y=[p[index * batch_size::], x[index * batch_size::], 
                                                    x[index * batch_size::], x[index * batch_size::]])
                index = 0
            else:
                loss = model.train_on_batch(x=[x[index * batch_size:(index + 1) * batch_size], 
                                               x[index * batch_size:(index + 1) * batch_size],
                                               x[index * batch_size:(index + 1) * batch_size]],
                                                 y=[p[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size],
                                                    x[index * batch_size:(index + 1) * batch_size]])
                index += 1
            ite += 1
        return y_pred

def autoencoder_lecun(dims, act='selu', uniqueID = '0', l1=False, dropout=False, init=None, noise=None):
    init_lecun = lecun_normal(seed=1)
    n_stacks = len(dims) - 1
    # input
    x = Input(shape=(dims[0],), name='input' + uniqueID)
    h = x
    # internal layers in encoder
    for i in range(n_stacks-1):
        h = Dense(units=dims[i + 1], activation=act, kernel_initializer=init_lecun, name='encoder_' + uniqueID +'%d' % i)(h)
    # hidden layer
    h = Dense(units=dims[-1], kernel_initializer=init_lecun, name='encoder_' + uniqueID +'%d' % (n_stacks - 1))(h)
    if l1:
        h = Lambda(lambda  x: K.l2_normalize(x, axis=1), name='encoder_a' + uniqueID +'%d' % (n_stacks - 1))(h)
    # internal layers in decoder
    for i in range(n_stacks-1, 0, -1):
        h = Dense(units=dims[i], activation=act, kernel_initializer=init_lecun, name='decoder_' + uniqueID +'%d' % i)(h)
        if dropout:
            h = AlphaDropout(0.3, name='dropout_' + uniqueID +'%d' % i, seed=1)(h)
    # output
    h = Dense(units=dims[0], kernel_initializer=init_lecun, name='decoder_' + uniqueID +'0')(h)
    return Model(inputs=x, outputs=h)

def autoencoder_batchnorm(dims, act='relu', uniqueID = '0', l1=False, init='glorot_uniform', noise=False, dropout=False):
    n_stacks = len(dims) - 1
    # input
    x = Input(shape=(dims[0],), name='input' + uniqueID)
    h = x
    # internal layers in encoder
    for i in range(n_stacks-1):
        h = Dense(units=dims[i + 1], kernel_initializer=init, name='encoder_' + uniqueID +'%d' % i)(h)
        h = BatchNormalization(axis=1)(h)
        h = Activation('relu')(h)
        if noise:
            h = GaussianNoise(stddev=1, name='noise_' + uniqueID +'%d' % i)(h)
    # hidden layer
    h = Dense(units=dims[-1], kernel_initializer=init, name='encoder_' + uniqueID +'%d' % (n_stacks - 1))(h)
    h = BatchNormalization(axis=1, name='encoder_a' + uniqueID +'%d' % (n_stacks - 1))(h)
    # internal layers in decoder
    for i in range(n_stacks-1, 0, -1):
        h = Dense(units=dims[i], activation=act, kernel_initializer=init, name='decoder_' + uniqueID +'%d' % i)(h)
        h = BatchNormalization(axis=1)(h)
        h = Activation('relu')(h)
        if dropout:
            h = GaussianDropout(0.3, name='dropout_' + uniqueID +'%d' % i)(h)
    # output
    h = Dense(units=dims[0], kernel_initializer=init, name='decoder_' + uniqueID +'0')(h)
    return Model(inputs=x, outputs=h)

def get_segment_num(x, y):
    import pwlf
    from GPyOpt.methods import BayesianOptimization
    # initialize piecewise linear fit with your x and y data
    my_pwlf = pwlf.PiecewiseLinFit(x, y)
    def my_obj(x):
        l = y.mean()*0.001
        f = np.zeros(x.shape[0])
        for i, j in enumerate(x):
            my_pwlf.fit(j[0])
            f[i] = my_pwlf.ssr + (l*j[0])
        return f
    # define the lower and upper bound for the number of line segments
    bounds = [{'name': 'var_1', 'type': 'discrete',
            'domain': np.arange(2, 4)}]
    np.random.seed(12121)
    myBopt = BayesianOptimization(my_obj, domain=bounds, model_type='GP',
                                initial_design_numdata=3,
                                initial_design_type='latin',
                                exact_feval=False, verbosity=False,
                                verbosity_model=False, num_core=10)
    max_iter = 50

    # perform the bayesian optimization to find the optimum number
    # of line segments
    myBopt.run_optimization(max_iter=max_iter, verbosity=False)
    return myBopt.x_opt

def get_rss(x, k):
    from scipy.spatial.distance import cdist
    kmeanModel = KMeans(n_clusters=k, random_state=None, n_init=20, n_jobs=-1)
    kmeanModel.fit(x)
    rss = sum(np.min(cdist(x, 
          kmeanModel.cluster_centers_, 'euclidean'), axis=1)) / x.shape[0]
    return rss

def get_cluster_num(h, maxK=30, plot_dir=None, i=1, subsampling_frac=1, subsampling_n=0):
    from sklearn.cluster import KMeans, MiniBatchKMeans
    import numpy as np
    import pwlf
    import matplotlib.pyplot as plt
    from joblib import Parallel, delayed

    if subsampling_frac < 1:
        np.random.seed(1)
        sampled_index = np.random.randint(0, h.shape[0], int(subsampling_frac*h.shape[0]))
        h = h[sampled_index, :]
    if subsampling_frac > 0:
        np.random.seed(1)
        sampled_index = np.random.randint(0, h.shape[0], 100000)
        h = h[sampled_index, :]
    distortions = []
    Ks = range(5, maxK+1)
    distortions = Parallel(n_jobs=len(Ks))(delayed(get_rss)(x=h, k=i) for i in Ks)

    segment_num = get_segment_num(np.array(Ks), np.array(distortions))
    # fit the data for four line segments
    my_pwlf = pwlf.PiecewiseLinFit(Ks, distortions)
    res = my_pwlf.fit(segment_num)
    print(res[int((segment_num[0]-1))])
    if plot_dir is not None:
        # predict for the determined points
        xHat = np.linspace(min(Ks), max(Ks), num=1000)
        yHat = my_pwlf.predict(xHat)
        plt.figure()
        plt.plot(Ks, distortions, 'o')
        plt.plot(xHat, yHat, '-')
        plt.savefig(plot_dir)
        plt.close()
    return np.ceil(res[int((segment_num[0]-1))])




#  experimental_run_tf_function=False)
def contractive_loss1(y_pred, y_true):
    mse = K.mean(K.square(y_true - y_pred), axis=1)
    
    lam = 1e-4
    model = ae1
    layer_names = [layer.name for layer in model.layers]
    embedding_ind = np.max(np.where(['encoder' in layer for layer in layer_names]))
    embedding_layer = layer_names[embedding_ind]
    
    W = model.get_layer(embedding_layer).weights[0]  # N x N_hidden
    W = K.transpose(W)  # N_hidden x N
    h = model.get_layer(embedding_layer).output
    dh = h * (1 - h)  # N_batch x N_hidden
    # N_batch x N_hidden * N_hidden x 1 = N_batch x 1
    contractive = lam * K.sum(dh**2 * K.sum(W**2, axis=1), axis=1)
    return mse + contractive
