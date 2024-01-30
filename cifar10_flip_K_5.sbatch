#!/usr/bin/env bash
#SBATCH --time=08:00:00
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu
#SBATCH --mem=8G
#SBATCH -J emlc_cifar10_flip_K_5_no_ssl
#SBATCH -o "/homes/evgeny/nobackup/log/emlc_cifar10_flip_K_5_no_ssl_%j.out"
#SBATCH -e "/homes/evgeny/nobackup/log/emlc_cifar10_flip_K_5_no_ssl_%j.err"

. ~/.bashrc
module load cuda/11.8.0
micromamba activate mlc
cd ~/code/EMLC
python main_cifar.py --dataset cifar10 --data_path ~/nobackup/cifar/ \
    --corruption_type flip --corruption_level 0.6 \
    --runid 0 --gradient_steps 5