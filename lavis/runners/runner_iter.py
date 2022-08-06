import datetime
import logging
import os
import time

import torch
import torch.distributed as dist
from lavis.common.dist_utils import is_main_process, main_process
from lavis.common.registry import registry
from lavis.runners.runner_base import RunnerBase
from torch.utils.data import DataLoader


@registry.register_runner("runner_iter")
class RunnerIter(RunnerBase):
    """
    Run training based on the number of iterations. This is common when
    the training dataset size is large. Underhood logic is similar to
    epoch-based training by considering every #iters_per_inner_epoch as an
    inner epoch.

    In iter-based runner, after every #iters_per_inner_epoch steps, we

        1) do a validation epoch;
        2) schedule the learning rate;
        3) save the checkpoint.

    We refer every #iters_per_inner_epoch steps as an inner epoch.
    """

    def __init__(self, cfg, task, model, datasets, job_id):
        super().__init__(cfg, task, model, datasets, job_id)

        self.max_iters = int(self.config.run_cfg.get("max_iters", -1))
        assert self.max_iters > 0, "max_iters must be greater than 0."

        self.iters_per_inner_epoch = int(
            self.config.run_cfg.get("iters_per_inner_epoch", -1)
        )
        assert (
            self.iters_per_inner_epoch > 0
        ), "iters_per_inner_epoch must be greater than 0."

    @property
    def max_epoch(self):
        return int(self.max_iters / self.iters_per_inner_epoch)

    @property
    def cur_epoch(self):
        try:
            return self.train_loader.epoch
        except AttributeError:
            # pipeline data (e.g. LAION) is streaming, have no concept of epoch
            return 0

    def _progress(self, cur_iters):
        return "{}_iters={}".format(self.cur_epoch, cur_iters)

    def train(self):
        start_time = time.time()
        best_agg_metric = 0
        best_iters = 0

        for start_iters in range(0, self.max_iters, self.iters_per_inner_epoch):
            end_iters = start_iters + self.iters_per_inner_epoch

            # training phase
            if not self.evaluate_only:
                logging.info(
                    "Start training, max_iters={}, in total {} inner epochs.".format(
                        self.max_iters, self.max_iters / self.iters_per_inner_epoch
                    )
                )

                train_stats = self.train_iters(self.cur_epoch, start_iters)
                self.log_stats(split_name="train", stats=train_stats)

            # evaluation phase
            if len(self.valid_splits) > 0:
                for split_name in self.valid_splits:
                    logging.info("Evaluating on {}.".format(split_name))

                    val_log = self.eval_epoch(
                        split_name=split_name, cur_epoch=self._progress(end_iters)
                    )
                    if val_log is not None:
                        if is_main_process():
                            assert (
                                "agg_metrics" in val_log
                            ), "No agg_metrics found in validation log."

                            agg_metrics = val_log["agg_metrics"]
                            if agg_metrics > best_agg_metric and split_name == "val":
                                best_iters, best_agg_metric = end_iters, agg_metrics

                                self.save_checkpoint(end_iters, is_best=True)

                            val_log.update({"best_iters": best_iters})
                            self.log_stats(val_log, split_name)

            else:
                # if no validation split is provided, we just save the checkpoint at the end of each inner epoch.
                if not self.evaluate_only:
                    self.save_checkpoint(end_iters, is_best=False)

            if self.evaluate_only:
                break
            dist.barrier()

        # testing phase
        self.evaluate(cur_epoch=self.cur_epoch)

        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        logging.info("Training time {}".format(total_time_str))

    def train_iters(self, epoch, start_iters):
        # train by iterations
        self.model.train()

        return self.task.train_iters(
            epoch=epoch,
            start_iters=start_iters,
            iters_per_inner_epoch=self.iters_per_inner_epoch,
            model=self.model,
            data_loader=self.train_loader,
            optimizer=self.optimizer,
            lr_scheduler=self.lr_scheduler,
            cuda_enabled=self.cuda_enabled,
            log_freq=self.log_freq,
        )

    @property
    def dataloaders(self):
        # [FIXME] this should be specified as an attribute of the dataset.
        use_datapipe = self.config.run_cfg.get("use_datapipe", False)

        if use_datapipe:
            # avoid creating samplers etc. for pipeline data
            if self._dataloaders is None:
                dataloaders = []

                split_names = sorted(self.datasets.keys())
                datasets = [self.datasets[split] for split in split_names]

                dataloaders = create_pipeline_loader(
                    datasets=datasets,
                    batch_size=[
                        self.config.run_cfg.batch_size_train
                        if split == "train"
                        else self.config.run_cfg.batch_size_eval
                        for split in split_names
                    ],
                    num_workers=[self.config.run_cfg.num_workers] * len(datasets),
                )

                self._dataloaders = {k: v for k, v in zip(split_names, dataloaders)}

            return self._dataloaders
        else:
            return super().dataloaders

    @main_process
    def save_checkpoint(self, cur_iters, is_best=False):
        save_obj = {
            "model": self.unwrap_dist_model(self.model).state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "config": self.config.to_dict(),
            "iters": cur_iters,
        }
        save_to = os.path.join(
            self.output_dir,
            "checkpoint_{}.pth".format("best" if is_best else cur_iters),
        )
        logging.info("Saving checkpoint at iters {} to {}.".format(cur_iters, save_to))
        torch.save(save_obj, save_to)


def create_pipeline_loader(datasets, batch_size, num_workers):
    loaders = []

    for dataset, batch_size, num_workers in zip(datasets, batch_size, num_workers):
        loaders.append(
            DataLoader(
                dataset,
                batch_size=batch_size,
                num_workers=num_workers,
                pin_memory=True,
            )
        )

    return loaders