#!/usr/bin/env bash

export PYTHONPATH=..:$PYTHONPATH
python test.py \
    configs/sem_fpn/PoolFormer/fpn_poolformer_s24_ade20k_40k.py \
    ../checkpoint/fpn_poolformer_s24_ade20k_40k.pth \
    --show-dir output \
