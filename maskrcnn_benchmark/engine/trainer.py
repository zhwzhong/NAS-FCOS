# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import datetime
import logging
import time

import torch
import torch.distributed as dist

from apex import amp
from maskrcnn_benchmark.utils.comm import get_world_size
from maskrcnn_benchmark.utils.metric_logger import MetricLogger


def reduce_loss_dict(loss_dict):
    """
    Reduce the loss dictionary from all processes so that process with rank
    0 has the averaged results. Returns a dict with the same fields as
    loss_dict, after reduction.
    """
    world_size = get_world_size()
    if world_size < 2:
        return loss_dict
    with torch.no_grad():
        loss_names = []
        all_losses = []
        for k in sorted(loss_dict.keys()):
            loss_names.append(k)
            all_losses.append(loss_dict[k])
        all_losses = torch.stack(all_losses, dim=0)
        dist.reduce(all_losses, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            all_losses /= world_size
        reduced_losses = {k: v for k, v in zip(loss_names, all_losses)}
    return reduced_losses


def visualize(visual_mode, optimizer, optimizer_split, meters, iteration, writer):
    """ 
    this function is used to record loss curve according to the specific post_branch
    """
    if optimizer_split:
        writer.add_scalar('train/enc_lr', optimizer.param_groups[0]["lr"], iteration)
        writer.add_scalar('train/dec_lr', optimizer.param_groups[1]["lr"], iteration)
    else:
        writer.add_scalar('train/lr', optimizer.param_groups[0]["lr"], iteration)
    writer.add_scalar('train/loss', meters.meters['loss'].avg, iteration)
    if visual_mode == 0 or visual_mode == 1:
        # visual_mode 0 and 1: Retina_Branch
        writer.add_scalar('train/loss_retina_cls', meters.meters['loss_retina_cls'].avg, iteration)
        writer.add_scalar('train/loss_retina_reg', meters.meters['loss_retina_reg'].avg, iteration)
    elif visual_mode == 2 or visual_mode == 3:
        # visual_mode 2 and 3: DenseBox_Branch
        writer.add_scalar('train/loss_densebox_cls', meters.meters['loss_densebox_cls'].avg, iteration)
        writer.add_scalar('train/loss_densebox_reg', meters.meters['loss_densebox_reg'].avg, iteration)
        writer.add_scalar('train/loss_reg_weights' , meters.meters['loss_reg_weights'].avg , iteration)
    else:
        # For common visual, only record lr and loss
        pass

def do_train(
    model,
    data_loader,
    optimizer,
    scheduler,
    checkpointer,
    device,
    checkpoint_period,
    arguments,
    writer,
):
    logger = logging.getLogger("maskrcnn_benchmark.trainer")
    logger.info("Start training")
    meters = MetricLogger(delimiter="  ")
    max_iter = len(data_loader)
    start_iter = arguments["iteration"]
    model.train()
    start_training_time = time.time()
    end = time.time()
    visual_mode = 5
    logger.info("data_loader.mode {}".format(data_loader.collate_fn.mode))
    logger.info("data_loader.special_deal {}".format(data_loader.collate_fn.special_deal))
    for iteration, (images, targets, _) in enumerate(data_loader, start_iter):
        data_time = time.time() - end
        iteration = iteration + 1
        arguments["iteration"] = iteration

        scheduler.step()

        images = images.to(device)
        targets = [target.to(device) for target in targets]

        try:
            loss_dict = model(images, targets)
        except ValueError as e:
            print("ValueError: {}".format(e))

        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(loss=losses_reduced, **loss_dict_reduced)

        optimizer.zero_grad()
        with amp.scale_loss(losses, optimizer) as scaled_loss:
            scaled_loss.backward()
        optimizer.step()

        batch_time = time.time() - end
        end = time.time()
        meters.update(time=batch_time, data=data_time)

        eta_seconds = meters.time.global_avg * (max_iter - iteration)
        eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

        if iteration % 20 == 0 or iteration == max_iter:
            logger.info(
                meters.delimiter.join(
                    [
                        "eta: {eta}",
                        "iter: {iter}",
                        "{meters}",
                        "lr: {lr:.6f}",
                        "max mem: {memory:.0f}",
                    ]
                ).format(
                    eta=eta_string,
                    iter=iteration,
                    meters=str(meters),
                    lr=optimizer.param_groups[0]["lr"],
                    memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                )
            )
            if writer is not None:
                visualize(visual_mode, optimizer, True, meters, iteration, writer)            
        if iteration % checkpoint_period == 0:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)
        if iteration == max_iter:
            checkpointer.save("model_final", **arguments)

    total_training_time = time.time() - start_training_time
    total_time_str = str(datetime.timedelta(seconds=total_training_time))
    logger.info(
        "Total training time: {} ({:.4f} s / it)".format(
            total_time_str, total_training_time / (max_iter)
        )
    )