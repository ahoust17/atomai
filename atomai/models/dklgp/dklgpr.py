"""
dklgpr.py
=========

Deep kernel learning (DKL)-based gaussian process regression (GPR)

Created by Maxim Ziatdinov (email: maxim.ziatdinov@ai4microscopy.com)
"""

import warnings
from typing import Tuple, Type, Union, List

import gpytorch
import numpy as np
import torch

from ...trainers import dklGPTrainer
from ...utils import init_dataloader

mvn_ = gpytorch.distributions.MultivariateNormal


class dklGPR(dklGPTrainer):
    """
    Deep kernel learning (DKL)-based Gaussian process regression (GPR)

    Args:
        indim: input feature dimension
        embedim: embedding dimension (determines dimensionality of kernel space)
        shared_embedding_space: use one embedding space for all target outputs

    Keyword Args:
        device:
            Sets device to which model and data will be moved.
            Defaults to 'cuda:0' if a GPU is available and to CPU otherwise.
        precision:
            Sets tensor types for 'single' (torch.float32)
            or 'double' (torch.float64) precision
        seed:
            Seed for enforcing reproducibility
    """
    def __init__(self,
                 indim: int,
                 embedim: int = 2,
                 shared_embedding_space: bool = True,
                 **kwargs: Union[str, int]) -> None:
        """
        Initializes DKL-GPR model
        """
        args = (indim, embedim, shared_embedding_space)
        super(dklGPR, self).__init__(*args, **kwargs)

    def fit(self, X: Union[torch.Tensor, np.ndarray],
            y: Union[torch.Tensor, np.ndarray],
            training_cycles: int = 1,
            **kwargs: Union[Type[torch.nn.Module], bool, float]
            ) -> None:
        """
        Initializes and trains a deep kernel GP model

        Args:
            X: Input training data (aka features) of N x input_dim dimensions
            y: Output targets of batch_size x N or N (if batch_size=1) dimensions
            training_cycles: Number of training epochs

        Keyword Args:
            feature_extractor:
                (Optional) Custom neural network for feature extractor
            freeze_weights:
                Freezes weights of feature extractor, that is, they are not
                passed to the optimizer. Used for a transfer learning.
            lr: learning rate (Default: 0.01)
            print_loss: print loss at every n-th training cycle (epoch)
        """
        _ = self.run(X, y, training_cycles, **kwargs)

    def _compute_posterior(self, X: torch.Tensor) -> Union[mvn_, List[mvn_]]:
        """
        Computes the posterior over model outputs at the provided points (X).
        """
        self.gp_model.eval()
        self.likelihood.eval()
        wrn = gpytorch.models.exact_gp.GPInputWarning
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=wrn)
            with torch.no_grad(), gpytorch.settings.use_toeplitz(False), gpytorch.settings.fast_pred_var():
                if self.correlated_output:
                    posterior = self.gp_model(X.to(self.device))
                else:
                    posterior = self.gp_model(*X.to(self.device).split(1))
        return posterior

    def sample_from_posterior(self, X, num_samples: int = 1000) -> np.ndarray:
        """
        Computes the posterior over model outputs at the provided points (X)
        and samples from it
        """
        X, _ = self.set_data(X)
        gp_batch_dim = len(self.gp_model.train_targets)
        X = X.expand(gp_batch_dim, *X.shape)
        posterior = self._compute_posterior(X)
        if self.correlated_output:
            samples = posterior.rsample(torch.Size([num_samples, ]))
        else:
            samples = [p.rsample(torch.Size([num_samples, ])) for p in posterior]
            samples = torch.cat(samples, 1)
        return samples.cpu().numpy()

    def _predict(self, x_new: torch.Tensor) -> Tuple[torch.Tensor]:
        posterior = self._compute_posterior(x_new)
        if self.correlated_output:
            return posterior.mean.cpu(), posterior.variance.cpu()
        means_all = torch.cat([p.mean for p in posterior])
        vars_all = torch.cat([p.variance for p in posterior])
        return means_all.cpu(), vars_all.cpu()

    def predict(self, x_new: Union[torch.Tensor, np.ndarray],
                **kwargs) -> Tuple[np.ndarray]:
        """
        Prediction of mean and variance using the trained model
        """
        gp_batch_dim = len(self.gp_model.train_targets)
        x_new, _ = self.set_data(x_new, device='cpu')
        data_loader = init_dataloader(x_new, shuffle=False, **kwargs)
        predicted_mean, predicted_var = [], []
        for (x,) in data_loader:
            x = x.expand(gp_batch_dim, *x.shape)
            mean, var = self._predict(x)
            predicted_mean.append(mean)
            predicted_var.append(var)
        return (torch.cat(predicted_mean, 1).numpy().squeeze(),
                torch.cat(predicted_var, 1).numpy().squeeze())

    def _embed(self, x_new: torch.Tensor):
        self.gp_model.eval()
        with torch.no_grad():
            if self.correlated_output:
                embeded = self.gp_model.feature_extractor(x_new)
                embeded = self.gp_model.scale_to_bounds(embeded)
            else:
                embeded = [m.scale_to_bounds(m.feature_extractor(x_new))[..., None]
                           for m in self.gp_model.models]
                embeded = torch.cat(embeded, -1)
        return embeded.cpu()

    def embed(self, x_new: Union[torch.Tensor, np.ndarray],
              **kwargs: int) -> torch.Tensor:
        """
        Embeds the input data to a "latent" space using a trained feature extractor NN.
        """
        x_new, _ = self.set_data(x_new, device='cpu')
        data_loader = init_dataloader(x_new, shuffle=False, **kwargs)
        embeded = torch.cat([self._embed(x.to(self.device)) for (x,) in data_loader], 0)
        if not self.correlated_output:
            embeded = embeded.permute(-1, 0, 1)
        return embeded.numpy()

    def decode(self, z_emb: Union[torch.Tensor, np.ndarray]) -> Tuple[torch.Tensor]:
        """
        "Decodes" the latent space variables into the target space using
        a trained Gaussian process model (i.e., "GP layer" of DKL-GP)
        """
        if not self.correlated_output:
            raise NotImplementedError(
                "Currently supports only models with shared embedding space")
        self.gp_model.eval()
        m = self.gp_model

        if m.prediction_strategy is None:
            _ = self._compute_posterior(m.train_inputs[0][:1])
        pstrategy = m.prediction_strategy.exact_prediction

        z_emb_training = m.feature_extractor(m.train_inputs[0])
        z_emb, _ = self.set_data(z_emb)
        z_emb = torch.cat([z_emb, z_emb_training], 0)
        z_emb = m.scale_to_bounds(z_emb)

        with torch.no_grad(), gpytorch.settings.use_toeplitz(False), gpytorch.settings.fast_pred_var():
            mean_z = m.mean_module(z_emb)
            covar_z = m.covar_module(z_emb)
            full_output = gpytorch.distributions.MultivariateNormal(mean_z, covar_z)
            full_mean, full_covar = full_output.loc, full_output.lazy_covariance_matrix
            with gpytorch.settings._use_eval_tolerance():
                pmean, pcovar = pstrategy(full_mean, full_covar)
            p = gpytorch.distributions.MultivariateNormal(pmean, pcovar)

        return p.mean.cpu().numpy().T, p.variance.cpu().numpy().T
