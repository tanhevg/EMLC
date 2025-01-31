# To run on Mac:
# PYTORCH_ENABLE_MPS_FALLBACK=1 python main_cifar.py --device mps --jvp_ad_method double-back-trick --dataset cifar10 --data_path ~/data/cifar/ --corruption_type flip --corruption_level 0.6 --runid 0 --gradient_steps 5
import pickle
import argparse
import os
import random
import numpy as np

import torch.multiprocessing as mp
from torch.distributed import destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP

from utils import get_logger, ddp_setup
from trainer import Trainer
from models import *
from meta_models import *
from CIFAR.data_helper_cifar import prepare_data

parser = argparse.ArgumentParser(description='EMLC Training Framework')

# General and paths
parser.add_argument('--jvp_ad_method', type=str, default='forward', choices=['forward', 'reverse', 'double-back-trick'])
parser.add_argument('--device', type=str, help="""
    Device to run on. If not set, we will assume a DDP setting, and figure out the devise based on rank.
    If set, rank will not be used, and DDP workers will not be spawned.
                    
""")
parser.add_argument('--dataset', type=str, choices=['cifar10', 'cifar100'], required=True) 
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--data_seed', type=int, default=0)
parser.add_argument('--runid', default=0, type=int)
parser.add_argument('--data_path', default='data/', type=str, help='Root for the datasets.')
parser.add_argument('--logdir', type=str, default='runs', help='Log folder.')
parser.add_argument('--ssl_path', type=str, help='SSL pretrained model path.')

# Training
parser.add_argument('--epochs', '-e', type=int, default=15, help='Number of epochs to train.')
parser.add_argument('--every', default=10, type=int, help='Eval interval')
parser.add_argument('--bs', default=128, type=int, help='batch size')
parser.add_argument('--test_bs', default=100, type=int, help='batch size')
parser.add_argument('--gold_bs', type=int, default=128)
parser.add_argument('--embedding_dim', type=int, default=512, help='Feature extractor output dim')
parser.add_argument('--label_embedding_dim', type=int, default=128, help='Label embedding dim')
parser.add_argument('--mlp_hidden_dim', type=int, default=128, help='MLP hidden layer units')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum for optimizer')
parser.add_argument('--main_lr', default=2e-2, type=float, help='lr for main net')
parser.add_argument('--meta_lr', default=2e-2, type=float, help='lr for meta net')
parser.add_argument('--wdecay', default=5e-4, type=float, help='weight decay')
parser.add_argument('--gradient_steps', default=5, type=int, help='Number of look-ahead gradient steps for meta-gradient')
parser.add_argument('--sched_milestones', default='20', type=str, help='Milestones in which LR is decreased')
parser.add_argument('--sched_gamma', default=0.1, type=float, help='Multiply LR by gamma upon reaching a scheduled milestone')

# Noise injection
parser.add_argument('--corruption_type', default='unif', type=str, choices=['unif', 'flip'])
parser.add_argument('--corruption_level', default=0.5, type=float, help='Corruption level')
parser.add_argument('--gold_fraction', default='0.02', type=float, help='Gold fraction')

# Hardware
parser.add_argument('--prefetch', type=int, default=2, help='Pre-fetching threads.')
parser.add_argument('--gpuid', default=0, type=int)
parser.add_argument('--n_gpus', default=1, type=int)

args = parser.parse_args()

# //////////////// set logging and model outputs /////////////////
def set_logging(rank):
    filename = '_'.join([args.dataset, str(args.corruption_level), args.corruption_type, str(args.runid), str(args.epochs), str(args.seed), str(args.data_seed)])
    if not os.path.isdir('logs'):
        os.mkdir('logs')
    logfile = 'logs/' + filename + '.log'
    logger = get_logger(logfile, rank)

    if not os.path.isdir('models'):
        os.mkdir('models')

    logger.info(args)
    return logger


# //////////////////////// defining model ////////////////////////

def build_models(rank, device, dataset, num_classes):
    ddp = device is None
    if ddp:
        device = rank
    args.embedding_dim = 512 if dataset == 'cifar10' else 2048
    model_fn = generalized_resnet34 if dataset == 'cifar10' else generalized_resnet50
    main_net = model_fn(num_classes, args, ssl=args.ssl_path is not None)
    meta_backbone = model_fn(num_classes, args, ssl=args.ssl_path is not None)

    meta_net = ResNetFeatures(meta_backbone)
    enhancer = TeacherEnhancer(num_classes, args.embedding_dim, args.label_embedding_dim, args.mlp_hidden_dim)

    main_net = main_net.to(device)
    meta_net = meta_net.to(device)
    enhancer = enhancer.to(device)

    if ddp:
        main_net = DDP(main_net, device_ids=[rank])
        meta_net = DDP(meta_net, device_ids=[rank])
        enhancer = DDP(enhancer, device_ids=[rank])
    
    return main_net, meta_net, enhancer

# //////////////////////// run experiments ////////////////////////
def run(rank):
    if args.device is None:
        ddp_setup(rank, world_size=args.n_gpus, runid=args.runid)
    logger = set_logging(rank)
    np.random.seed(args.seed)
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    filename = '_'.join([args.dataset, args.corruption_type, str(args.runid), str(args.epochs), str(args.seed), str(args.data_seed)])
    exp_id = filename

    results = {}

    assert args.gold_fraction >=0 and args.gold_fraction <=1, 'Wrong gold fraction!'
    assert args.corruption_level >= 0 and args.corruption_level <=1, 'Wrong noise level!'

    gold_loader, silver_loader, valid_loader, test_loader, num_classes = prepare_data(args.gold_fraction, args.corruption_level, args)
    main_net, meta_net, enhacner = build_models(rank, args.device, args.dataset, num_classes)

    trainer = Trainer(rank, args, main_net, meta_net, enhacner,
                    gold_loader, silver_loader, valid_loader, test_loader,
                    num_classes, logger, 'none', False, exp_id)
    trainer.train()
    test_acc = trainer.final_eval()
    
    if rank == 0:
        results['method'] = test_acc
        logger.info(' '.join(['Gold fraction:', str(args.gold_fraction), '| Corruption level:', str(args.corruption_level),
            '| Method acc:', str(results['method'])]))
        logger.info('')

    if rank == 0:
        with open('out/' + filename, 'wb') as file:
            pickle.dump(results, file)
    logger.info("Dumped results_ours in file: " + filename)
    destroy_process_group()

if __name__ == "__main__":
    if args.device is None:
        gpus = range(args.gpuid, args.gpuid+args.n_gpus)
        os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(str(x) for x in gpus)
        mp.spawn(run, nprocs=args.n_gpus)
    else:
        run(0)