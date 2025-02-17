# Copyright (c) 2015-present, Facebook, Inc.
# All rights reserved.
"""
Train and eval functions used in main.py
"""
import math
import os.path
import sys
from typing import Iterable, Optional

import numpy as np
import torch
import torchvision
from matplotlib import pyplot as plt

from timm.data import Mixup
from timm.utils import accuracy, ModelEma

from losses import DistillationLoss
import utils
from torch.utils.tensorboard import SummaryWriter

from Visualizer.visualizer import local_cache


def train_one_epoch(model: torch.nn.Module, criterion: DistillationLoss,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler, max_norm: float = 0,
                    model_ema: Optional[ModelEma] = None, mixup_fn: Optional[Mixup] = None,
                    set_training_mode=True, log_writer:Optional[SummaryWriter]=None):
    model.train(set_training_mode)
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10

    if log_writer is not None:
        print('log_dir: {}'.format(log_writer.log_dir))

    for data_iter_step, (samples, targets) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        samples = samples.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)

        if mixup_fn is not None:
            samples, targets = mixup_fn(samples, targets)

        with torch.cuda.amp.autocast():
            outputs = model(samples)
            loss = criterion(samples, outputs, targets)

        loss_value = loss.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            sys.exit(1)

        optimizer.zero_grad()

        # this attribute is added by timm on one optimizer (adahessian)
        is_second_order = hasattr(optimizer, 'is_second_order') and optimizer.is_second_order
        loss_scaler(loss, optimizer, clip_grad=max_norm,
                    parameters=model.parameters(), create_graph=is_second_order)

        torch.cuda.synchronize()
        if model_ema is not None:
            model_ema.update(model)

        metric_logger.update(loss=loss_value)
        
        lr = optimizer.param_groups[0]["lr"]
        metric_logger.update(lr=lr)

        loss_value_reduce = utils.all_reduce_mean(loss_value)
        if log_writer is not None:
            """ We use epoch_1000x as the x-axis in tensorboard.
            This calibrates different curves when batch size changes.
            """
            epoch_1000x = int((data_iter_step / len(data_loader) + epoch) * 1000)
            log_writer.add_scalar('loss', loss_value_reduce, epoch_1000x)
            log_writer.add_scalar('lr', lr, epoch_1000x)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate(data_loader, model, device, ori_dataset=None):
    def set_times_new_roman_font():
        from matplotlib import rcParams

        config = {
            "font.family": 'serif',
            "font.size": 12,
            "font.serif": ['Times New Roman'],
            # "mathtext.fontset": 'stix',
            # 'axes.unicode_minus': False
        }
        rcParams.update(config)
    set_times_new_roman_font()

    # visualize the cnn layer in downsample
    if ori_dataset is not None:
        for name, param in model.named_parameters():
            if 'downsample_layers.0.0.weight' == name:
                in_channels = param.size()[1]  # 输入通道
                out_channels = param.size()[0]  # 输出通道
                k_w, k_h = param.size()[3], param.size()[2]  # 卷积核的尺寸
                kernel_all = param.view(-1, 1, k_w, k_h)  # 每个通道的卷积核
                kernel_grid = torchvision.utils.make_grid(kernel_all, normalize=True, scale_each=True, nrow=in_channels)
                plt.figure(figsize=(10, 10))
                plt.imshow(kernel_grid.permute(1, 2, 0).cpu().numpy(), cmap='viridis')
                plt.title("Convolutional Kernels")
                plt.axis('off')
                # plt.show()
                plt.savefig('out/visual/convolutional kernels in downsample 0.png')

    criterion = torch.nn.CrossEntropyLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    # switch to evaluation mode
    model.eval()

    batch_num = 0
    for images, target in metric_logger.log_every(data_loader, 10, header):  # [192, 3, 224, 224], [192]
        images = images.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)

        # compute output
        local_cache.clear()
        with torch.cuda.amp.autocast():
            output = model(images)
            if ori_dataset is not None:
                if len(target) >= 100:
                    # get cache and phase the data
                    cache = local_cache.cache
                    x = cache['Block.local_images.images'][0]  # ndarray: [192, 384, 14, 14]
                    r_weight = cache['Block.local_r_weight.r_weight'][0]  # ndarray: [192, 49, 16]
                    r_idx = cache['Block.local_r_idx.r_idx'][0]  # ndarray: [192, 49, 16]
                    attn_weight = cache['Block.local_attn_weight.attn_weight'][0]  # ndarray: [192 * 49, 12, 4, 64]

                    # get parameters
                    batch_size = x.shape[0]  # 192
                    win_size = attn_weight.shape[2]  # 4

                    # mean & reshape
                    attn_weight = attn_weight.mean(axis=1)  # [192 * 49, 4, 64]
                    attn_weight = attn_weight.reshape(batch_size, r_weight.shape[1] * win_size, -1)  # [192, 49 * 4, 64]
                    attn_weight = attn_weight.mean(axis=2)  # [192, 49 * 4]

                    # visualize images
                    for alpha in [0.2]:
                        plt.clf()
                        fig, axes = plt.subplots(nrows=10, ncols=10, figsize=(42, 42))
                        for i in range(100):
                            # get original image
                            image, label = ori_dataset[batch_size * batch_num + i]

                            # check label
                            if label == target[i]:
                                attention_map = attn_weight[i]  # attn size need less than figure size
                                length = int(math.sqrt(attention_map.shape[0]))
                                attention_map = attention_map.reshape(length, length)
                                attention_map = np.repeat(attention_map, 3, axis=0)
                                attention_map = np.repeat(attention_map, 3, axis=1)
                                image = torchvision.transforms.ToPILImage()(image)

                                ax = axes[i // 10, i % 10]
                                image = image.resize((42, 42))
                                ax.imshow(image)
                                ax.imshow(attention_map, alpha=alpha, cmap='rainbow')
                                ax.axis('off')

                        plt.tight_layout()
                        # plt.show()
                        path = 'out/visual'
                        if not os.path.exists(path):
                            os.makedirs(path)
                        plt.savefig(f'out/visual/figure and attn {batch_num} alpha {alpha}.png')
                        print(f'out/visual/figure and attn {batch_num} alpha {alpha}.png saved')

                    batch_num = batch_num + 1

            loss = criterion(output, target)

        acc1, acc5 = accuracy(output, target, topk=(1, 5))

        batch_size = images.shape[0]
        metric_logger.update(loss=loss.item())
        metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
        metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
          .format(top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}
