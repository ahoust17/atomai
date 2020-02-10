"""
Module for training neural networks
and making predictions with trained models
"""
import os
import time

import numpy as np
import torch
import torch.nn.functional as F

from atomai.losses import dice_loss as dice
from atomai.models import dilnet, dilUnet
from atomai.utils import (torch_format, load_model, Hook, mock_forward,
                          plot_losses, img_pad, img_resize)


class net_train:
    """
    Class for training a fully convolutional neural network
    for semantic segmentation of noisy experimental data

    Args:
        images_all: list / dict / 4D numpy array
            list or dictionary of 4D numpy arrays or 4D numpy array
            (3D image tensors stacked along the first dim)
            representing training images
        labels_all: list / dict / 4D numpy array
            list or dictionary of 3D numpy arrays or 
            4D (binary) / 3D (multiclass) numpy array
            where 3D / 2D image are tensors stacked along the first dim
            which represent training labels (aka masks aka ground truth)
        images_test_all: list / dict / 4D numpy array
            list or dictionary of 4D numpy arrays or 4D numpy array
            (3D image tensors stacked along the first dim)
            representing test images
        labels_test_all: list / dict / 4D numpy array
            list or dictionary of 3D numpy arrays or 
            4D (binary) / 3D (multiclass) numpy array
            where 3D / 2D image are tensors stacked along the first dim
            which represent test labels (aka masks aka ground truth)
        training_cycles: int
            number of training 'epochs' (1 epoch == 1 batch)
        batch_size: int
            size of training and test batches
        model: pytorch object with neural network skeleton
            allows using custom modules from outside this package
        model_type: str
            Type of mode to choose from the package ('dilUnet' or 'dilnet')
        use_batchnorm: bool
            Apply batch normalization after each convolutional layer
        use_dropouts: bool
            Apply dropouts in the three inner blocks in the middle of a network
        loss: str
            type of loss for model training ('ce' or 'dice')
        with_dilation: bool
            use / not use dilated convolutions in the bottleneck of dilUnet
        print_loss: int
            prints loss every n-th epoch
        savedir:
            directory to automatically save intermediate and final weights
    """
    def __init__(self,
                 images_all,
                 labels_all,
                 images_test_all,
                 labels_test_all,
                 training_cycles,
                 batch_size=32,
                 model=None,
                 model_type='dilUnet',
                 use_batchnorm=True,
                 use_dropouts=False,
                 loss="dice",
                 with_dilation=True,
                 print_loss=100,
                 savedir='./',
                 plot_losses=True):

        assert type(images_all) == type(labels_all)\
        == type(images_test_all) == type(labels_test_all),\
        "Provide all training and test image/labels data in the same format"
        if type(labels_all) == list:
            num_classes = set([len(np.unique(lab)) for lab in labels_all])
        elif type(labels_all) == dict:
            num_classes = set(
                [len(np.unique(lab)) for lab in labels_all.values()])
        elif type(labels_all) == np.ndarray:
            n_train_batches, _ = np.divmod(labels_all.shape[0], batch_size)
            n_test_batches, _ = np.divmod(labels_test_all.shape[0], batch_size)
            images_all = np.split(images_all, n_train_batches)
            labels_all = np.split(labels_all, n_train_batches)
            images_test_all = np.split(images_test_all, n_test_batches)
            labels_test_all = np.split(labels_test_all, n_test_batches)
            num_classes = set([len(np.unique(lab)) for lab in labels_all])    
        else:
            raise NotImplementedError(
                "Provide training and test data as python list (or dictionary)",
                "of numpy arrays or as 4D (images)",
                "and 4D/3D (labels for binary/multiclass) numpy arrays"
            )
        assert len(num_classes) == 1,\
         "Confirm that all ground truth images has the same number of classes"
        num_classes = num_classes.pop()
        assert num_classes != 1,\
        "Confirm that you have a class corresponding to background"
        num_classes = num_classes - 1 if num_classes == 2 else num_classes
        if model_type == 'dilUnet':
            self.net = dilUnet(
                num_classes, 16, with_dilation, use_dropouts, use_batchnorm
            )
        elif model_type == 'dilnet':
            self.net = dilnet(num_classes, 25, use_dropouts, use_batchnorm)
        else:
            raise NotImplementedError(
                "Currently implemented models are 'dilUnet' and 'dilnet'"
            )
        torch.cuda.empty_cache()
        self.net.cuda()
        if loss == 'dice':
            self.criterion = dice()
        elif loss == 'ce' and num_classes == 1:
            self.criterion = torch.nn.BCEWithLogitsLoss()
        elif loss == 'ce' and num_classes > 2:
            self.criterion = torch.nn.CrossEntropyLoss()
        else:
            raise NotImplementedError(
                "Choose between Dice loss ('dice') and cross-entropy loss ('ce')"
            )
        self.batch_idx_train = np.random.randint(
            0, len(images_all), training_cycles)
        self.batch_idx_test = np.random.randint(
            0, len(images_test_all), training_cycles)
        self.optimizer = torch.optim.Adam(self.net.parameters(), lr=1e-3)
        self.images_all = images_all
        self.labels_all = labels_all
        self.images_test_all = images_test_all
        self.labels_test_all = labels_test_all
        self.num_classes = num_classes
        self.batch_size = batch_size
        self.training_cycles = training_cycles
        self.print_loss = print_loss
        self.savedir = savedir
        self.plot_training_history = plot_training_history
        self.train_loss, self.test_loss = [], []
        self.run()

    def dataloader(self, images_all, labels_all, batch_num):
        # Generate batch of training images with corresponding ground truth
        images = images_all[batch_num][:self.batch_size]
        labels = labels_all[batch_num][:self.batch_size]
        # Transform images and ground truth to torch tensors and move to GPU
        images = torch.from_numpy(images).float()
        if self.num_classes == 1:
            labels = torch.from_numpy(labels).float()
        else:
            labels = torch.from_numpy(labels).long()
        images, labels = images.cuda(), labels.cuda()
        return images, labels

    def train_step(self, img, lbl):
        """Forward --> Backward --> Optimize"""
        self.net.train()
        self.optimizer.zero_grad()
        prob = self.net.forward(img)
        loss = self.criterion(prob, lbl)
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def test_step(self, img, lbl):
        """Forward path for test data"""
        self.net.eval()
        with torch.no_grad():
            prob = self.net.forward(img)
            loss = self.criterion(prob, lbl)
        return loss.item()

    def run(self):
        for e in range(self.training_cycles):
            # Get training images/labels
            images, labels = self.dataloader(
                self.images_all, self.labels_all,
                self.batch_idx_train[e])
            # Training step
            loss = self.train_step(images, labels)
            self.train_loss.append(loss)
            images_, labels_ = self.dataloader(
                self.images_test_all, self.labels_test_all,
                self.batch_idx_test[e])
            loss_ = self.test_step(images_, labels_)
            self.test_loss.append(loss_)
            # Print loss info
            if e == 0 or (e+1) % self.print_loss == 0:
                print('Epoch {} ...'.format(e+1),
                      'Training loss: {} ...'.format(
                          np.around(self.train_loss[-1], 4)),
                      'Test loss: {}'.format(
                    np.around(self.test_loss[-1], 4)))
            # Save model weights (if test loss decreased)
            if e > 0 and self.test_loss[-1] < min(self.test_loss[: -1]):
                torch.save(self.net.state_dict(),
                   os.path.join(self.savedir, 'model_test_weights_best.pt'))
        # Save final model weights
        torch.save(self.net.state_dict(),
                   os.path.join(self.savedir, 'model_weights_final.pt'))
        # Run evaluation (by passing all the test data) on the final model state
        running_loss_test = 0
        for idx in range(len(self.images_test_all)):
            images_, labels_ = self.dataloader(
                self.images_test_all, self.labels_test_all, idx)
            loss = self.test_step(images_, labels_)
            running_loss_test += loss
        print('Model (final state) evaluation loss:',
              np.around(running_loss_test/len(self.images_test_all), 4))
        if self.plot_training_history:
            plot_losses(self.train_loss, self.test_loss)
        return self.net


class net_predict:
    """
    Predictions with a trained neural network

    Args:
        image_data: 2D or 3D numpy array
            image stack or a single image (all greyscale)
        model: object
            trained pytorch model (skeleton+weights)
        model_skeleton: pytorch object with neural network skeleton
            The path to saved weights must be provided (see 'model_weights' arg)
        model_weights: pytorch model state dict
            Must match with the tensor dimension in the 'model skeleton' arg
        resize: tuple / 2-element list
            target dimensions for optional image(s) resizing

        Kwargs:
            **nb_classes: int
                number of classes in the model
            **downsampled: int or float
                downsampling factor
            **use_gpu: bool
                optional use of gpu device for inference
     """
    def __init__(self,
                 image_data,
                 trained_model=None,
                 model_skeleton=None,
                 model_weights=None,
                 resize=None,
                 use_gpu=False,
                 **kwargs):

        if trained_model is None:
            assert model_skeleton and model_weights,\
            "Load both model skeleton and weights path"
            assert model_skeleton.__dict__, "Load a valid pytorch model skeleton"
            assert isinstance(model_weights, str), "Filepath must be a string"
            model = load_model(model_skeleton, model_weights)
        else:
            model = trained_model
        self.nb_classes = kwargs.get('nb_classes', None)
        if self.nb_classes is None:
            hookF = [Hook(layer[1]) for layer in list(model._modules.items())]
            mock_forward(model)
            self.nb_classes = [hook.output.shape for hook in hookF][-1][1]
        downsampling = kwargs.get('downsampling', None)
        if downsampling is None:
            hookF = [Hook(layer[1]) for layer in list(model._modules.items())]
            mock_forward(model)
            imsize = [hook.output.shape[-1] for hook in hookF]
            downsampling = max(imsize)/min(imsize)
        self.model = model
        if use_gpu:
            self.model.cuda()
        else:
            self.model.cpu()
        if image_data.ndim == 2:
            image_data = np.expand_dims(image_data, axis=0)
        if resize is not None:
            image_data = img_resize(image_data, resize)
        image_data = img_pad(image_data, downsampling)
        self.image_data = torch_format(image_data)
        self.use_gpu = use_gpu
        self.run()

    def predict(self, images):
        '''Returns 'probability' of each pixel
           in image belonging to an atom'''
        if self.use_gpu:
            images = images.cuda()
        self.model.eval()
        with torch.no_grad():
            prob = self.model.forward(images)
            if self.nb_classes > 1:
                prob = F.softmax(prob, dim=1)
            else:
                prob = torch.sigmoid(prob)
        if self.use_gpu:
            images = images.cpu()
            prob = prob.cpu()
        prob = prob.permute(0, 2, 3, 1) # reshape to have channel as a last dim
        prob = prob.numpy()
        return prob

    def run(self):
        '''Make prediction'''
        start_time = time.time()
        if self.image_data.shape[0] < 20 and min(self.image_data.shape[2:4]) < 512:
            decoded_imgs = self.predict(self.image_data)
        else:
            n, _, w, h = self.image_data.shape
            decoded_imgs = np.zeros((n, w, h, self.nb_classes))
            for i in range(n):
                decoded_imgs[i, :, :, :] = self.predict(self.image_data[i:i+1])
        n_images_str = " image was " if decoded_imgs.shape[0] == 1 else " images were "
        print(str(decoded_imgs.shape[0])
                + n_images_str + "decoded in approximately "
                + str(np.around(time.time() - start_time, decimals=4))
                + ' seconds')
        images_numpy = self.image_data.permute(0, 2, 3, 1).numpy()
        return images_numpy, decoded_imgs