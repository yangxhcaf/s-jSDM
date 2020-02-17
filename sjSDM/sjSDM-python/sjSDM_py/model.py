import torch
import numpy as np
import sys
from .utils_fa import _device_and_dtype


class Model_base:
    """Base Model in fajsm

    Base model class for joint species distribution models
    based on the multivariate probit model.
    The likelihood is numerically approximated by Monte-Carlo
    integreation

    # Example
    
        >>> python
        >>> model = Model_base(5)
        >>> model.add_layer(Layer_dense(10))
        >>> model.build(5, optimizer_Adamax(0.01))
        >>> model.fit(X, Y, epochs = 10, step_size = 10)

    # Arguments
    :param input_shape: integer > 0, number of environmental covariates
    :param device: str, which device "cpu" or "gpu"
    :param dtype: str, which dtype, float32 or float64

    """
    def __init__(self, input_shape=None, device="cpu", dtype="float32"):
        self.input_shape = int(input_shape)
        self.layers = []
        self.weights = []
        self.losses = []
        self.history = None
        self.loss_function = None
        self.sigma = None
        self.sigma_numpy = None
        self.weights_numpy = []
        self.optimizer = None
        self.sigma_constraint = None
        self.df = None
        device, dtype = self._device_and_dtype(device, dtype)
        self.device = device
        self.dtype = dtype

    def __repr__(self):
        return "Model_base: \n  {} \n".format(self.input_shape)

    def __call__(self, input):
        self.predict(newdata=input)
    
    _device_and_dtype = _device_and_dtype

    def get_cov(self):
        """Return covariance matrix

        Returns the species-species association (covariance) matrix

        """
        return torch.matmul(self.sigma, self.sigma.t()).data.cpu().numpy()

    def get_sigma(self):
        """returns sigma

        Returns sigma as torch.tensor

        """
        return self.sigma

    def set_weights(self, weights):
        """set weights in model

        set weights (list of lists of numpy arrays)

        # Arguments
            weights: list, list of list of numpy arrays

        """
        for i, w in enumerate(weights):
            self.layers[i].set_weights(w)

    def get_sigma_numpy(self):
        return self.sigma.data.cpu().numpy()

    def add_layer(self, layer):
        """Adds a layer to the model

        Add a layer to the model

        # Arguments
            layer: Layer_dense, must be a Layer_dense object

        """
        if len(self.layers) == 0:
            layer._set_shape(self.input_shape)
        else:
            layer._set_shape(self.layers[-1].get_shape()[1])
        self.layers.append(layer)

    def build(self, df=None, optimizer=None, l1=0.0, l2=0.0,
              reg_on_Cov=True, reg_on_Diag=True, inverse=False):
        """Build model

        Initialize and build the model.

        # Arguments
        :param df: int, species-species association matrix's degree of freedom
        :param optimizer: optimizer_function, e.g. optimizer_Adamax
        :param l1: float > 0.0, lasso penality on covariances
        :param l2: float > 0.0, ride penality on covariances
        :param reg_on_Cov: logical, regularization on covariance matrix or directly on sigma
        :param reg_on_Diag: logical, regularization on diagonals

        """
        if self.df == None:
            self.df = int(df)
        else:
            df = self.df
        r_dim = self.layers[-1].get_shape()[1]

        low = -np.sqrt(6.0/(r_dim+df))
        high = np.sqrt(6.0/(r_dim+df))
                                
        self.sigma = torch.tensor(np.random.uniform(low, high, [r_dim, df]), requires_grad = True, dtype = self.dtype, device = self.device).to(self.device)
        
        for i in range(0,len(self.layers)):
            self.layers[i].build(device = self.device, dtype = self.dtype)
            self.weights.append(self.layers[i].get_weights())
            if self.layers[i].get_loss() != None :
                self.losses.append(self.layers[i].get_loss())
        
        self.__loss_function = self.__build_loss_function()
        self.__build_cov_constrain_function(l1 = l1, l2 = l2, reg_on_Cov = reg_on_Cov, reg_on_Diag = reg_on_Diag, inverse = inverse)
        params = [y for x in self.weights for y in x]
        params.append(self.sigma)
        if optimizer != None:
            self.optimizer = optimizer(params = params)
    
    def fit(self, X=None, Y=None, batch_size=25, epochs=100, sampling=100, parallel=0):
        """fit model

        Fit the model

        # Arguments
        :param X: 2D-numpy array, environmental covariates
        :param Y: 2D-numpy array, species responses
        :param batch_size: int of 1, batch size for stochastic gradient descent
        :param epochs: int of 1, number of iterations to fit the model on the data
        :param sampling: int of 1, sampling parameter for the Monte-Carlo Integreation
        :param parallel: int of 1, number of workers for the dataLoader

        # Example

            >>> X = np.random.randn(100,5)
            >>> Y = np.random.binomial(1,0.5, [100, 10])
            >>> model = Model_base(5)
            >>> model.add_layer(Layer_dense(10))
            >>> model.build(10, optimizer_adamax(0.1))
            >>> model.fit(X, Y, batch_size=25, epochs=10)

        """
        stepSize = np.floor(X.shape[0] / batch_size).astype(int)
        #steps = stepSize * epochs

        dataLoader = self._get_DataLoader(X, Y, batch_size, True, parallel)
        any_losses = len(self.losses) > 0
        any_layers = len(self.layers) > 0

        batch_loss = torch.zeros(stepSize, device = self.device, dtype = self.dtype).to(self.device)
        self.history = np.zeros(epochs)
        
        for epoch in range(epochs):
            for step, (x, y) in enumerate(dataLoader):
                x = x.to(self.device, non_blocking=True)
                y = y.to(self.device, non_blocking=True)
                mu = self.layers[0](x)
                if any_layers:
                    for i in range(1, len(self.layers)):
                        mu = self.layers[i](mu)
                
                loss = self.__loss_function(mu, y, batch_size, sampling)
                loss = torch.mean(loss)
                if any_losses:
                    for k in range(len(self.losses)):
                        loss += self.losses[k]()
                
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                batch_loss[step].data = loss.data
            torch.cuda.empty_cache()
            bl = np.mean(loss.data.cpu().numpy())
            _ = sys.stdout.write("\rEpoch: {}/{} loss: {} ".format(epoch+1,epochs, np.round(bl, 3).astype(str)))
            sys.stdout.flush()
            self.history[epoch] = bl
            
        self.sigma_numpy = self.get_sigma_numpy()
        torch.cuda.empty_cache()
        for layer in self.layers:
            self.weights_numpy.append(layer.get_weights_numpy())
        torch.cuda.empty_cache()
    
    def predict(self, newdata=None, train=False, batch_size=25, parallel=0, sampling=100):
        """predict for newdata
        
        Predict on newdata in batches

        :param newdata: 2D-numpy array, environmental data
        :param train: logical of 1, in case of dropout layer -> train state
        :param batch_size: int of 1, newdata will be split into batches
        :param sampling: int of 1, sampling parameter for the Monte-Carlo Integreation
        :param parallel: int of 1, number of workers for the dataLoader

        """
        dataLoader = self._get_DataLoader(X = newdata, Y = None, batch_size = batch_size, shuffle = False, parallel = parallel, drop_last = False)
        loss_function = self.__build_loss_function(train = False)
        any_layers = len(self.layers) > 0
        pred = []
        for _, x in enumerate(dataLoader):
            x = x[0].to(self.device, non_blocking=True)
            mu = self.layers[0](x)
            if any_layers:
                for i in range(1, len(self.layers)):
                    mu = self.layers[i](mu)
            loss = loss_function(mu,  x.shape[0], sampling)
            pred.append(loss)
        predictions = torch.cat(pred, dim = 0).data.cpu().numpy()
        return predictions

    def logLik(self, X, Y, batch_size=25, parallel=0, sampling=100):
        """Returns log-likelihood of model

        :param X: 2D-numpy array, environemntal predictors
        :param Y: 2D-numpy array, species responses
        :param batch_size: int of 1, newdata will be split into batches
        :param sampling: int of 1, sampling parameter for the Monte-Carlo Integreation
        :param parallel: int of 1, number of workers for the dataLoader

        """
        dataLoader = self._get_DataLoader(X = X, Y = Y, batch_size = batch_size, shuffle = False, parallel = parallel, drop_last = False)
        loss_function = self.__build_loss_function(train = True)
        torch.cuda.empty_cache()
        any_losses = len(self.losses) > 0
        any_layers = len(self.layers) > 0
        logLik = 0
        logLikReg = 0
        for step, (x, y) in enumerate(dataLoader):
            x = x.to(self.device, non_blocking=True)
            y = y.to(self.device, non_blocking=True)
            mu = self.layers[0](x)
            if any_layers:
                for i in range(1, len(self.layers)):
                    mu = self.layers[i](mu)
            loss = loss_function(mu, y, batch_size, sampling)
            loss = torch.sum(loss)
            loss_reg = torch.tensor(0.0, dtype=self.dtype, device=self.device).to(self.device)
            if any_losses:
                for k in range(len(self.losses)):
                    loss_reg += self.losses[k]()
                logLikReg += loss_reg.data.cpu().numpy()
            logLik += loss.data.cpu().numpy()
            torch.cuda.empty_cache()
        return logLik, logLikReg

    def _get_DataLoader(self, X, Y=None, batch_size=25, shuffle=True, parallel=0, drop_last=True):
        if self.device.type == 'cuda':
            torch.cuda.set_device(self.device)
            pin_memory = False
        else:
            pin_memory = True
        #init_func = lambda: torch.multiprocessing.set_start_method('spawn', True)
        if type(Y) is np.ndarray:
            data = torch.utils.data.TensorDataset(torch.tensor(X, dtype=self.dtype, device=torch.device('cpu')), torch.tensor(Y, dtype=self.dtype, device=torch.device('cpu')))
        else:
            data = torch.utils.data.TensorDataset(torch.tensor(X, dtype=self.dtype, device=torch.device('cpu')))

        DataLoader = torch.utils.data.DataLoader(data, batch_size=batch_size, shuffle=shuffle, num_workers=int(parallel), pin_memory=pin_memory, drop_last=drop_last)
        torch.cuda.empty_cache()
        return DataLoader

    def __build_cov_constrain_function(self, l1=None, l2=None, reg_on_Cov=None, reg_on_Diag=None, inverse=None):
        if reg_on_Cov:
            if reg_on_Diag:
                diag = int(0)
            else:
                diag = int(1)
            
            if l1 > 0.0:
                l1 = torch.tensor(l1, device = self.device, dtype = self.dtype).to(self.device)
                def l1_ll():
                    ss = torch.matmul(self.sigma, self.sigma.t())
                    if inverse:
                        ss = torch.inverse(ss)
                    return torch.mul(l1, torch.sum(torch.abs(torch.triu(ss, diag))))
                self.losses.append(l1_ll)
            
            if l2 > 0.0 :
                l2 = torch.tensor(l2, device = self.device, dtype = self.dtype).to(self.device)
                def l2_ll():
                    ss = torch.matmul(self.sigma, self.sigma.t())
                    if inverse:
                        ss = torch.inverse(ss)
                    return torch.mul(l2, torch.sum(torch.pow(torch.triu(ss, diag), 2.0)))
                self.losses.append(l2_ll)
        else:
            if l1 > 0.0:
                l1 = torch.tensor(l1, device = self.device, dtype = self.dtype).to(self.device)
                self.losses.append(lambda: torch.mul(l1, torch.sum(torch.abs(self.sigma))))
            if l2 > 0.0:
                l2 = torch.tensor(l2, device = self.device, dtype = self.dtype).to(self.device)
                self.losses.append(lambda: torch.mul(l2, torch.sum(torch.pow(self.sigma, 2.0))))
        return None
    
    def __build_loss_function(self, train=True):
        eps = torch.tensor(0.00001, dtype=self.dtype).to(self.device)
        zero = torch.tensor(0.0, dtype=self.dtype).to(self.device)
        one = torch.tensor(1.0, dtype=self.dtype).to(self.device)
        alpha = torch.tensor(1.70169, dtype=self.dtype).to(self.device)
        half = torch.tensor(0.5, dtype=self.dtype).to(self.device)
        
        if train:
            def tmp(mu, Ys, batch_size, sampling):
                noise = torch.randn(size = [sampling, batch_size, self.df],dtype = self.dtype, device = self.device)
                samples = torch.add(torch.tensordot(noise, self.sigma.t(), dims = 1), mu)
                E = torch.add(torch.mul(torch.sigmoid(torch.mul(alpha, samples)) , torch.sub(one,eps)), torch.mul(eps, half))
                indll = torch.neg(torch.add(torch.mul(torch.log(E), Ys), torch.mul(torch.log(torch.sub(one,E)),torch.sub(one,Ys))))
                logprob = torch.neg(torch.sum(indll, dim = 2))
                maxlogprob = torch.max(logprob, dim = 0).values
                Eprob = torch.mean(torch.exp(torch.sub(logprob,maxlogprob)), dim = 0)
                loss = torch.sub(torch.neg(torch.log(Eprob)),maxlogprob)
                return loss
        else:
            def tmp(mu, batch_size, sampling):
                noise = torch.randn(size = [sampling, batch_size, self.df],dtype = self.dtype, device = self.device)
                samples = torch.add(torch.tensordot(noise, self.sigma.t(), dims = 1), mu)
                E = torch.add(torch.mul(torch.sigmoid(torch.mul(alpha, samples)) , torch.sub(one,eps)), torch.mul(eps, half))
                return torch.mean(E, dim = 0)

        return tmp