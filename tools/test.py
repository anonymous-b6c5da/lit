import argparse
import datetime
import glob
import os
import re
import time
from pathlib import Path

import numpy as np
import torch
import torch.distributed as dist
from eval_utils import eval_utils
from tensorboardX import SummaryWriter

from lit.path_utils import LitPaths
from pcdet.config import cfg, cfg_from_list, cfg_from_yaml_file, log_config_to_file
from pcdet.datasets import build_dataloader
from pcdet.models import build_network
from pcdet.models.model_utils.dsnorm import DSNorm
from pcdet.utils import common_utils


def parse_config():
    parser = argparse.ArgumentParser(description="arg parser")
    parser.add_argument(
        "--cfg_file", type=str, default=None, help="specify the config for training"
    )

    parser.add_argument(
        "--batch_size",
        type=int,
        default=16,
        required=False,
        help="batch size for training",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=80,
        required=False,
        help="Number of epochs to train for",
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="number of workers for dataloader"
    )
    parser.add_argument(
        "--extra_tag", type=str, default="default", help="extra tag for this experiment"
    )
    parser.add_argument(
        "--ckpt", type=str, default=None, help="checkpoint to start from"
    )
    parser.add_argument(
        "--launcher", choices=["none", "pytorch", "slurm"], default="none"
    )
    parser.add_argument(
        "--tcp_port", type=int, default=18888, help="tcp port for distrbuted training"
    )
    parser.add_argument(
        "--local_rank", type=int, default=0, help="local rank for distributed training"
    )
    parser.add_argument(
        "--set",
        dest="set_cfgs",
        default=None,
        nargs=argparse.REMAINDER,
        help="set extra config keys if needed",
    )

    parser.add_argument(
        "--max_waiting_mins", type=int, default=30, help="max waiting minutes"
    )
    parser.add_argument("--start_epoch", type=int, default=0, help="")
    parser.add_argument(
        "--eval_tag", type=str, default="default", help="eval tag for this experiment"
    )
    parser.add_argument(
        "--eval_all",
        action="store_true",
        default=False,
        help="whether to evaluate all checkpoints",
    )
    parser.add_argument(
        "--ckpt_dir",
        type=str,
        default=None,
        help="specify a ckpt directory to be evaluated if needed",
    )
    parser.add_argument("--save_to_file", action="store_true", default=False, help="")

    args = parser.parse_args()

    cfg_from_yaml_file(args.cfg_file, cfg)
    cfg.TAG = Path(args.cfg_file).stem
    cfg.EXP_GROUP_PATH = "/".join(
        args.cfg_file.split("/")[1:-1]
    )  # remove 'cfgs' and 'xxxx.yaml'

    np.random.seed(1024)

    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs, cfg)

    return args, cfg


def eval_single_ckpt(
    model, test_loader, args, eval_output_dir, logger, epoch_id, dist_test=False
):
    # Load checkpoint
    model.load_params_from_file(filename=args.ckpt, logger=logger, to_cpu=dist_test)
    model.cuda()

    # Get metric path
    ckpt_path = Path(args.ckpt)
    ckpt_dir = ckpt_path.parent
    if dist_test:
        metric_dir = ckpt_dir.parent / "metric_dist_test"
    else:
        metric_dir = ckpt_dir.parent / "metric_test"
    metric_path = metric_dir / f"{ckpt_path.stem}.txt"

    if metric_path.is_file() and metric_path.stat().st_size > 0:
        print(f"Skip evaluation because {metric_path} exists and is not empty")
        return

    # Start evaluation
    ret_dict, result_str = eval_utils.eval_one_epoch(
        cfg,
        model,
        test_loader,
        epoch_id,
        logger,
        dist_test=dist_test,
        result_dir=eval_output_dir,
        save_to_file=args.save_to_file,
        args=args,
    )

    # Save result_str to metric_path
    metric_path.parent.mkdir(parents=True, exist_ok=True)
    with open(metric_path, "w") as f:
        f.write(result_str)


def get_no_evaluated_ckpt(ckpt_dir, ckpt_record_file, args):
    ckpt_list = glob.glob(os.path.join(ckpt_dir, "*checkpoint_epoch_*.pth"))
    ckpt_list.sort(key=os.path.getmtime)
    evaluated_ckpt_list = [
        float(x.strip()) for x in open(ckpt_record_file, "r").readlines()
    ]

    for cur_ckpt in ckpt_list:
        num_list = re.findall("checkpoint_epoch_(.*).pth", cur_ckpt)
        if num_list.__len__() == 0:
            continue

        epoch_id = num_list[-1]
        if "optim" in epoch_id:
            continue
        if (
            float(epoch_id) not in evaluated_ckpt_list
            and int(float(epoch_id)) >= args.start_epoch
        ):
            return epoch_id, cur_ckpt
    return -1, None


def repeat_eval_ckpt(
    model, test_loader, args, eval_output_dir, logger, ckpt_dir, dist_test=False
):
    # Tensorboard log
    if cfg.LOCAL_RANK == 0:
        tb_log = SummaryWriter(
            log_dir=str(
                eval_output_dir
                / ("tensorboard_%s" % cfg.DATA_CONFIG.DATA_SPLIT["test"])
            )
        )

    # Get a list of non-evaluated checkpoints. If metric_path exists and the
    # file is not empty, the checkpoint is regarded as evaluated.
    #
    # Example:
    #   - ckpt_path  : {ckpt_dir}/checkpoint_epoch_1.pth
    #   - metric_path: {ckpt_dir}/../"metric"/checkpoint_epoch_1.txt
    ckpt_paths = glob.glob(os.path.join(ckpt_dir, "*checkpoint_epoch_*.pth"))
    ckpt_paths.sort(key=os.path.getmtime)
    ckpt_paths = [Path(p) for p in ckpt_paths]
    if dist_test:
        metric_dir = ckpt_dir.parent / "metric_dist_test"
    else:
        metric_dir = ckpt_dir.parent / "metric_test"

    print(f"Found {len(ckpt_paths)} checkpoints =====================")
    to_evaluate_ckpt_paths = []
    for ckpt_path in ckpt_paths:
        metric_path = metric_dir / f"{ckpt_path.stem}.txt"
        if metric_path.is_file() and metric_path.stat().st_size > 0:
            print(f"- Evaluated  : {ckpt_path}")
        else:
            to_evaluate_ckpt_paths.append(ckpt_path)
            print(f"- To evaluate: {ckpt_path}")
    print("===========================================================")

    # Evaluate non-evaluated checkpoints
    for ckpt_path in to_evaluate_ckpt_paths:
        # Get epoch_id
        epoch_id = int(ckpt_path.stem.split("_")[-1])

        # Load checkpoint
        model.load_params_from_file(filename=ckpt_path, logger=logger, to_cpu=dist_test)
        model.cuda()

        # Start evaluation
        result_dir = (
            eval_output_dir
            / ("epoch_%s" % epoch_id)
            / cfg.DATA_CONFIG.DATA_SPLIT["test"]
        )
        ret_dict, result_str = eval_utils.eval_one_epoch(
            cfg,
            model,
            test_loader,
            epoch_id,
            logger,
            dist_test=dist_test,
            result_dir=result_dir,
            save_to_file=args.save_to_file,
            args=args,
        )

        if cfg.LOCAL_RANK == 0:
            for key, val in ret_dict.items():
                tb_log.add_scalar(key, val, epoch_id)

        # Save result_str to metric_path
        if cfg.LOCAL_RANK == 0:
            metric_path = metric_dir / f"{ckpt_path.stem}.txt"
            metric_path.parent.mkdir(parents=True, exist_ok=True)
            with open(metric_path, "w") as f:
                f.write(result_str)

    if cfg.LOCAL_RANK == 0:
        tb_log.flush()


def main():
    args, cfg = parse_config()
    if args.launcher == "none":
        dist_test = False
        total_gpus = 1
    else:
        total_gpus, cfg.LOCAL_RANK = getattr(
            common_utils, "init_dist_%s" % args.launcher
        )(args.tcp_port, args.local_rank, backend="nccl")
        dist_test = True

    if args.batch_size is None:
        args.batch_size = cfg.OPTIMIZATION.BATCH_SIZE_PER_GPU
    else:
        assert (
            args.batch_size % total_gpus == 0
        ), "Batch size should match the number of gpus"
        args.batch_size = args.batch_size // total_gpus

    output_dir = cfg.ROOT_DIR / "output" / cfg.EXP_GROUP_PATH / cfg.TAG / args.extra_tag
    output_dir.mkdir(parents=True, exist_ok=True)

    eval_output_dir = output_dir / "eval"

    if not args.eval_all:
        num_list = re.findall(r"\d+", args.ckpt) if args.ckpt is not None else []
        epoch_id = num_list[-1] if num_list.__len__() > 0 else "no_number"
        eval_output_dir = (
            eval_output_dir
            / ("epoch_%s" % epoch_id)
            / cfg.DATA_CONFIG.DATA_SPLIT["test"]
        )
    else:
        eval_output_dir = eval_output_dir / "eval_all_default"

    if args.eval_tag is not None:
        eval_output_dir = eval_output_dir / args.eval_tag

    eval_output_dir.mkdir(parents=True, exist_ok=True)
    log_file = eval_output_dir / (
        "log_eval_%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    logger = common_utils.create_logger(log_file, rank=cfg.LOCAL_RANK)

    # log to file
    logger.info("**********************Start logging**********************")
    gpu_list = (
        os.environ["CUDA_VISIBLE_DEVICES"]
        if "CUDA_VISIBLE_DEVICES" in os.environ.keys()
        else "ALL"
    )
    logger.info("CUDA_VISIBLE_DEVICES=%s" % gpu_list)

    if dist_test:
        logger.info("total_batch_size: %d" % (total_gpus * args.batch_size))
    for key, val in vars(args).items():
        logger.info("{:16} {}".format(key, val))
    log_config_to_file(cfg, logger=logger)

    ckpt_dir = args.ckpt_dir if args.ckpt_dir is not None else output_dir / "ckpt"

    if cfg.get("DATA_CONFIG_TAR", None):
        test_set, test_loader, sampler = build_dataloader(
            dataset_cfg=cfg.DATA_CONFIG_TAR,
            class_names=cfg.DATA_CONFIG_TAR.CLASS_NAMES,
            batch_size=args.batch_size,
            dist=dist_test,
            workers=args.workers,
            logger=logger,
            training=False,
        )
    else:
        test_set, test_loader, sampler = build_dataloader(
            dataset_cfg=cfg.DATA_CONFIG,
            class_names=cfg.CLASS_NAMES,
            batch_size=args.batch_size,
            dist=dist_test,
            workers=args.workers,
            logger=logger,
            training=False,
        )

    model = build_network(
        model_cfg=cfg.MODEL, num_class=len(cfg.CLASS_NAMES), dataset=test_set
    )

    if cfg.get("SELF_TRAIN", None) and cfg.SELF_TRAIN.get("DSNORM", None):
        model = DSNorm.convert_dsnorm(model)

    state_name = "model_state"

    with torch.no_grad():
        if args.eval_all:
            repeat_eval_ckpt(
                model,
                test_loader,
                args,
                eval_output_dir,
                logger,
                ckpt_dir,
                dist_test=dist_test,
            )
        else:
            eval_single_ckpt(
                model,
                test_loader,
                args,
                eval_output_dir,
                logger,
                epoch_id,
                dist_test=dist_test,
            )


if __name__ == "__main__":
    main()
