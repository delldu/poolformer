# Copyright 2021 Garena Online Private Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
PoolFormer implementation
"""
import os
import copy
import torch
import torch.nn as nn

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.layers import DropPath, trunc_normal_
from timm.models.registry import register_model
from timm.models.layers.helpers import to_2tuple

import pdb

try:
    from mmseg.models.builder import BACKBONES as seg_BACKBONES
    from mmseg.utils import get_root_logger
    from mmcv.runner import _load_checkpoint
    has_mmseg = True
except ImportError:
    print("If for semantic segmentation, please install mmsegmentation first")
    has_mmseg = False

try:
    from mmdet.models.builder import BACKBONES as det_BACKBONES
    from mmdet.utils import get_root_logger
    from mmcv.runner import _load_checkpoint
    has_mmdet = True
except ImportError:
    print("If for detection, please install mmdetection first")
    has_mmdet = False


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .95, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD, 
        'classifier': 'head',
        **kwargs
    }


default_cfgs = {
    'poolformer_s': _cfg(crop_pct=0.9),
    'poolformer_m': _cfg(crop_pct=0.95),
}


class PatchEmbed(nn.Module):
    """
    Patch Embedding that is implemented by a layer of conv. 
    Input: tensor in shape [B, C, H, W]
    Output: tensor in shape [B, C, H/stride, W/stride]
    """
    def __init__(self, patch_size=16, stride=16, padding=0, 
                 in_chans=3, embed_dim=768, norm_layer=None):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        stride = to_2tuple(stride)
        padding = to_2tuple(padding)
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, 
                              stride=stride, padding=padding)
        self.norm = norm_layer(embed_dim) if norm_layer else nn.Identity()

        # self = PatchEmbed(
        #   (proj): Conv2d(3, 64, kernel_size=(7, 7), stride=(4, 4), padding=(2, 2))
        #   (norm): Identity()
        # )
        # patch_size = (7, 7)
        # stride = (4, 4)
        # padding = (2, 2)
        # in_chans = 3
        # embed_dim = 64
        # norm_layer = None

        # self.proj -- Conv2d(3, 64, kernel_size=(7, 7), stride=(4, 4), padding=(2, 2))
        # == self.norm -- Identity()

    def forward(self, x):
        # x.size() -- torch.Size([128, 3, 224, 224])
        x = self.proj(x)
        x = self.norm(x)
        # torch.Size([128, 64, 56, 56])
        return x


class LayerNormChannel(nn.Module):
    """
    LayerNorm only for Channel Dimension.
    Input: tensor in shape [B, C, H, W]
    """
    def __init__(self, num_channels, eps=1e-05):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(num_channels))
        self.bias = nn.Parameter(torch.zeros(num_channels))
        self.eps = eps
        pdb.set_trace()

    def forward(self, x):
        u = x.mean(1, keepdim=True)
        s = (x - u).pow(2).mean(1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.eps)
        x = self.weight.unsqueeze(-1).unsqueeze(-1) * x \
            + self.bias.unsqueeze(-1).unsqueeze(-1)
        return x


class GroupNorm(nn.GroupNorm):
    """
    Group Normalization with 1 group.
    Input: tensor in shape [B, C, H, W]
    """
    def __init__(self, num_channels, **kwargs):
        super().__init__(1, num_channels, **kwargs)
        # self = GroupNorm(1, 64, eps=1e-05, affine=True)
        # num_channels = 64
        # kwargs = {}


class Pooling(nn.Module):
    """
    Implementation of pooling for PoolFormer
    --pool_size: pooling size
    """
    def __init__(self, pool_size=3):
        super().__init__()
        self.pool = nn.AvgPool2d(
            pool_size, stride=1, padding=pool_size//2, count_include_pad=False)
        # self = Pooling(
        #   (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
        # )
        # pool_size = 3


    def forward(self, x):
        # x.size() -- torch.Size([128, 64, 56, 56])
        # ==> self.pool(x) - x ----torch.Size([128, 64, 56, 56])
        return self.pool(x) - x


class Mlp(nn.Module):
    """
    Implementation of MLP with 1*1 convolutions.
    Input: tensor with shape [B, C, H, W]
    """
    def __init__(self, in_features, hidden_features=None, 
                 out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Conv2d(in_features, hidden_features, 1)
        self.act = act_layer()
        self.fc2 = nn.Conv2d(hidden_features, out_features, 1)
        self.drop = nn.Dropout(drop)
        self.apply(self._init_weights)

        # self = Mlp(
        #   (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
        #   (act): GELU()
        #   (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
        #   (drop): Dropout(p=0.0, inplace=False)
        # )
        # in_features = 64
        # hidden_features = 256
        # out_features = 64
        # act_layer = <class 'torch.nn.modules.activation.GELU'>
        # drop = 0.0

    def _init_weights(self, m):
        if isinstance(m, nn.Conv2d):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        # x.size() -- torch.Size([128, 64, 56, 56])

        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        # x.size() -- torch.Size([128, 64, 56, 56])

        return x


class PoolFormerBlock(nn.Module):
    """
    Implementation of one PoolFormer block.
    --dim: embedding dim
    --pool_size: pooling size
    --mlp_ratio: mlp expansion ratio
    --act_layer: activation
    --norm_layer: normalization
    --drop: dropout rate
    --drop path: Stochastic Depth, 
        refer to https://arxiv.org/abs/1603.09382
    --use_layer_scale, --layer_scale_init_value: LayerScale, 
        refer to https://arxiv.org/abs/2103.17239
    """
    def __init__(self, dim, pool_size=3, mlp_ratio=4., 
                 act_layer=nn.GELU, norm_layer=GroupNorm, 
                 drop=0., drop_path=0., 
                 use_layer_scale=True, layer_scale_init_value=1e-5):

        super().__init__()

        self.norm1 = norm_layer(dim)
        self.token_mixer = Pooling(pool_size=pool_size)
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, 
                       act_layer=act_layer, drop=drop)

        # The following two techniques are useful to train deep PoolFormers.
        self.drop_path = DropPath(drop_path) if drop_path > 0. \
            else nn.Identity()
        self.use_layer_scale = use_layer_scale

        # use_layer_scale -- True
        if use_layer_scale:
            self.layer_scale_1 = nn.Parameter(
                layer_scale_init_value * torch.ones((dim)), requires_grad=True)
            self.layer_scale_2 = nn.Parameter(
                layer_scale_init_value * torch.ones((dim)), requires_grad=True)

        # self = PoolFormerBlock(
        #   (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
        #   (token_mixer): Pooling(
        #     (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
        #   )
        #   (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
        #   (mlp): Mlp(
        #     (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
        #     (act): GELU()
        #     (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
        #     (drop): Dropout(p=0.0, inplace=False)
        #   )
        #   (drop_path): Identity()
        # )
        # dim = 64
        # pool_size = 3
        # mlp_ratio = 4
        # drop = 0.0
        # drop_path = 0.0
        # use_layer_scale = True
        # layer_scale_init_value = 1e-05

    def forward(self, x):
        # torch.Size([128, 64, 56, 56])
        # self.use_layer_scale -- True
        # self.drop_path -- Identity()

        # self.layer_scale_1.size() -- torch.Size([64])
        # self.layer_scale_2.size() -- torch.Size([64])
        # self.norm1(x).size() -- torch.Size([128, 64, 56, 56])
        # self.token_mixer(self.norm1(x)).size() -- torch.Size([128, 64, 56, 56])
        # self.mlp(self.norm2(x)).size() -- torch.Size([128, 64, 56, 56])

        if self.use_layer_scale:
            x = x + self.drop_path(
                self.layer_scale_1.unsqueeze(-1).unsqueeze(-1)
                * self.token_mixer(self.norm1(x)))
            x = x + self.drop_path(
                self.layer_scale_2.unsqueeze(-1).unsqueeze(-1)
                * self.mlp(self.norm2(x)))
        else:
            x = x + self.drop_path(self.token_mixer(self.norm1(x)))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        # x.size() -- torch.Size([128, 64, 56, 56])
        return x


def basic_blocks(dim, index, layers, 
                 pool_size=3, mlp_ratio=4., 
                 act_layer=nn.GELU, norm_layer=GroupNorm, 
                 drop_rate=.0, drop_path_rate=0., 
                 use_layer_scale=True, layer_scale_init_value=1e-5):
    """
    generate PoolFormer blocks for a stage
    return: PoolFormer blocks 
    """
    blocks = []
    for block_idx in range(layers[index]):
        block_dpr = drop_path_rate * (
            block_idx + sum(layers[:index])) / (sum(layers) - 1)
        blocks.append(PoolFormerBlock(
            dim, pool_size=pool_size, mlp_ratio=mlp_ratio, 
            act_layer=act_layer, norm_layer=norm_layer, 
            drop=drop_rate, drop_path=block_dpr, 
            use_layer_scale=use_layer_scale, 
            layer_scale_init_value=layer_scale_init_value, 
            ))
    blocks = nn.Sequential(*blocks)

    # Sequential(
    #   (0): PoolFormerBlock(
    #     (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (token_mixer): Pooling(
    #       (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #     )
    #     (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (mlp): Mlp(
    #       (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #       (act): GELU()
    #       (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #       (drop): Dropout(p=0.0, inplace=False)
    #     )
    #     (drop_path): Identity()
    #   )
    #   (1): PoolFormerBlock(
    #     (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (token_mixer): Pooling(
    #       (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #     )
    #     (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (mlp): Mlp(
    #       (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #       (act): GELU()
    #       (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #       (drop): Dropout(p=0.0, inplace=False)
    #     )
    #     (drop_path): Identity()
    #   )
    #   (2): PoolFormerBlock(
    #     (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (token_mixer): Pooling(
    #       (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #     )
    #     (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (mlp): Mlp(
    #       (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #       (act): GELU()
    #       (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #       (drop): Dropout(p=0.0, inplace=False)
    #     )
    #     (drop_path): Identity()
    #   )
    #   (3): PoolFormerBlock(
    #     (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (token_mixer): Pooling(
    #       (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #     )
    #     (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #     (mlp): Mlp(
    #       (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #       (act): GELU()
    #       (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #       (drop): Dropout(p=0.0, inplace=False)
    #     )
    #     (drop_path): Identity()
    #   )
    # )

    return blocks


class PoolFormer(nn.Module):
    """
    PoolFormer, the main class of our model
    --layers: [x,x,x,x], number of blocks for the 4 stages
    --embed_dims, --mlp_ratios, --pool_size: the embedding dims, mlp ratios and 
        pooling size for the 4 stages
    --downsamples: flags to apply downsampling or not
    --norm_layer, --act_layer: define the types of normalizaiotn and activation
    --num_classes: number of classes for the image classification
    --in_patch_size, --in_stride, --in_pad: specify the patch embedding
        for the input image
    --down_patch_size --down_stride --down_pad: 
        specify the downsample (patch embed.)
    --fork_faat: whetehr output features of the 4 stages, for dense prediction
    --init_cfg，--pretrained: 
        for mmdetection and mmsegmentation to load pretrianfed weights
    """
    def __init__(self, layers, embed_dims=None, 
                 mlp_ratios=None, downsamples=None, 
                 pool_size=3, 
                 norm_layer=GroupNorm, act_layer=nn.GELU, 
                 num_classes=1000,
                 in_patch_size=7, in_stride=4, in_pad=2, 
                 down_patch_size=3, down_stride=2, down_pad=1, 
                 drop_rate=0., drop_path_rate=0.,
                 use_layer_scale=True, layer_scale_init_value=1e-5, 
                 fork_feat=False,
                 init_cfg=None, 
                 pretrained=None, 
                 **kwargs):

        super().__init__()
        # self = PoolFormer(
        #   (patch_embed): PatchEmbed(
        #     (proj): Conv2d(3, 64, kernel_size=(7, 7), stride=(4, 4), padding=(2, 2))
        #     (norm): Identity()
        #   )
        # )
        # layers = [4, 4, 12, 4]
        # embed_dims = [64, 128, 320, 512]
        # mlp_ratios = [4, 4, 4, 4]
        # downsamples = [True, True, True, True]
        # pool_size = 3
        # num_classes = 1000
        # in_patch_size = 7
        # in_stride = 4
        # in_pad = 2
        # down_patch_size = 3
        # down_stride = 2
        # down_pad = 1
        # drop_rate = 0.0
        # drop_path_rate = 0.0
        # use_layer_scale = True
        # layer_scale_init_value = 1e-05
        # fork_feat = False
        # init_cfg = None
        # pretrained = None
        # kwargs = {'in_chans': 3}


        if not fork_feat:
            self.num_classes = num_classes
        self.fork_feat = fork_feat

        self.patch_embed = PatchEmbed(
            patch_size=in_patch_size, stride=in_stride, padding=in_pad, 
            in_chans=3, embed_dim=embed_dims[0])

        # set the main block in network
        network = []
        # layers -- [4, 4, 12, 4]
        # embed_dims -- [64, 128, 320, 512]
        # mlp_ratios -- [4, 4, 4, 4]
        for i in range(len(layers)):
            stage = basic_blocks(embed_dims[i], i, layers, 
                                 pool_size=pool_size, mlp_ratio=mlp_ratios[i],
                                 act_layer=act_layer, norm_layer=norm_layer, 
                                 drop_rate=drop_rate, 
                                 drop_path_rate=drop_path_rate,
                                 use_layer_scale=use_layer_scale, 
                                 layer_scale_init_value=layer_scale_init_value)
            network.append(stage)
            if i >= len(layers) - 1:
                break
            if downsamples[i] or embed_dims[i] != embed_dims[i+1]:
                # downsampling between two stages
                network.append(
                    PatchEmbed(
                        patch_size=down_patch_size, stride=down_stride, 
                        padding=down_pad, 
                        in_chans=embed_dims[i], embed_dim=embed_dims[i+1]
                        )
                    )

        self.network = nn.ModuleList(network)

        # self.fork_feat -- False
        if self.fork_feat:
            # add a norm layer for each output
            self.out_indices = [0, 2, 4, 6]
            for i_emb, i_layer in enumerate(self.out_indices):
                if i_emb == 0 and os.environ.get('FORK_LAST3', None):
                    # TODO: more elegant way
                    """For RetinaNet, `start_level=1`. The first norm layer will not used.
                    cmd: `FORK_LAST3=1 python -m torch.distributed.launch ...`
                    """
                    layer = nn.Identity()
                else:
                    layer = norm_layer(embed_dims[i_emb])
                layer_name = f'norm{i_layer}'
                self.add_module(layer_name, layer)
        else:
            # Classifier head
            # embed_dims[-1] -- 512
            self.norm = norm_layer(embed_dims[-1])
            # self.norm -- GroupNorm(1, 512, eps=1e-05, affine=True)
            self.head = nn.Linear(
                embed_dims[-1], num_classes) if num_classes > 0 \
                else nn.Identity()
            # self.head -- Linear(in_features=512, out_features=1000, bias=True)

        self.apply(self.cls_init_weights)

        self.init_cfg = copy.deepcopy(init_cfg)
        # load pre-trained model 
        # self.fork_feat -- False
        if self.fork_feat and (
                self.init_cfg is not None or pretrained is not None):
            self.init_weights()


    # init for classification
    def cls_init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)

    # init for mmdetection or mmsegmentation by loading 
    # imagenet pre-trained weights
    def init_weights(self, pretrained=None):
        logger = get_root_logger()
        if self.init_cfg is None and pretrained is None:
            logger.warn(f'No pre-trained weights for '
                        f'{self.__class__.__name__}, '
                        f'training start from scratch')
            pass
        else:
            assert 'checkpoint' in self.init_cfg, f'Only support ' \
                                                  f'specify `Pretrained` in ' \
                                                  f'`init_cfg` in ' \
                                                  f'{self.__class__.__name__} '
            if self.init_cfg is not None:
                ckpt_path = self.init_cfg['checkpoint']
            elif pretrained is not None:
                ckpt_path = pretrained

            ckpt = _load_checkpoint(
                ckpt_path, logger=logger, map_location='cpu')
            if 'state_dict' in ckpt:
                _state_dict = ckpt['state_dict']
            elif 'model' in ckpt:
                _state_dict = ckpt['model']
            else:
                _state_dict = ckpt

            state_dict = _state_dict
            missing_keys, unexpected_keys = \
                self.load_state_dict(state_dict, False)
            
            # show for debug
            # print('missing_keys: ', missing_keys)
            # print('unexpected_keys: ', unexpected_keys)

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes):
        self.num_classes = num_classes
        self.head = nn.Linear(
            self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_embeddings(self, x):
        # pdb.set_trace()
        x = self.patch_embed(x)
        # pdb.set_trace()
        torch.cuda.empty_cache()

        return x

    def forward_tokens(self, x):
        # pdb.set_trace()
        # self.fork_feat -- False
        outs = []
        for idx, block in enumerate(self.network):
            x = block(x)
            if self.fork_feat and idx in self.out_indices:
                norm_layer = getattr(self, f'norm{idx}')
                x_out = norm_layer(x)
                outs.append(x_out)
            torch.cuda.empty_cache()

        if self.fork_feat:
            # output the features of four stages for dense prediction
            return outs
        # output only the features of last layer for image classification
        # pdb.set_trace()

        torch.cuda.empty_cache()

        return x

    def forward(self, x):
        # x.size() -- torch.Size([128, 3, 224, 224])
        # input embedding
        x = self.forward_embeddings(x)
        # x.size() -- torch.Size([128, 64, 56, 56])
        torch.cuda.empty_cache()

        # through backbone
        x = self.forward_tokens(x)
        # x.size() -- torch.Size([128, 512, 7, 7])
        torch.cuda.empty_cache()

        # self.fork_feat -- False
        if self.fork_feat:
            # otuput features of four stages for dense prediction
            return x
        x = self.norm(x)
        # x.size() -- torch.Size([128, 512, 7, 7])

        torch.cuda.empty_cache()

        cls_out = self.head(x.mean([-2, -1]))

        # for image classification
        # cls_out.size() -- torch.Size([128, 1000])

        return cls_out


@register_model
def poolformer_s12(pretrained=False, **kwargs):
    """
    PoolFormer-S12 model, Params: 12M
    --layers: [x,x,x,x], numbers of layers for the four stages
    --embed_dims, --mlp_ratios: 
        embedding dims and mlp ratios for the four stages
    --downsamples: flags to apply downsampling or not in four blocks
    """
    layers = [2, 2, 6, 2]
    embed_dims = [64, 128, 320, 512]
    mlp_ratios = [4, 4, 4, 4]
    downsamples = [True, True, True, True]
    model = PoolFormer(
        layers, embed_dims=embed_dims, 
        mlp_ratios=mlp_ratios, downsamples=downsamples, 
        **kwargs)
    model.default_cfg = default_cfgs['poolformer_s']
    return model


@register_model
def poolformer_s24(pretrained=False, **kwargs):
    """
    PoolFormer-S24 model, Params: 21M
    """
    layers = [4, 4, 12, 4]
    embed_dims = [64, 128, 320, 512]
    mlp_ratios = [4, 4, 4, 4]
    downsamples = [True, True, True, True]
    model = PoolFormer(
        layers, embed_dims=embed_dims, 
        mlp_ratios=mlp_ratios, downsamples=downsamples, 
        **kwargs)

    model.default_cfg = default_cfgs['poolformer_s']

    # model.default_cfg -- {'url': '', 'num_classes': 1000, 
    # 'input_size': (3, 224, 224), 'pool_size': None, 
    # 'crop_pct': 0.9, 'interpolation': 'bicubic', 
    # 'mean': (0.485, 0.456, 0.406), 
    # 'std': (0.229, 0.224, 0.225), 'classifier': 'head'}

    # (Pdb) pp model
    # PoolFormer(
    #   (patch_embed): PatchEmbed(
    #     (proj): Conv2d(3, 64, kernel_size=(7, 7), stride=(4, 4), padding=(2, 2))
    #     (norm): Identity()
    #   )
    #   (network): ModuleList(
    #     (0): Sequential(
    #       (0): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (1): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (2): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (3): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 64, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(64, 256, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(256, 64, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #     )
    #     (1): PatchEmbed(
    #       (proj): Conv2d(64, 128, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1))
    #       (norm): Identity()
    #     )
    #     (2): Sequential(
    #       (0): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (1): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (2): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (3): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 128, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(128, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(512, 128, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #     )
    #     (3): PatchEmbed(
    #       (proj): Conv2d(128, 320, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1))
    #       (norm): Identity()
    #     )
    #     (4): Sequential(
    #       (0): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (1): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (2): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (3): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (4): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (5): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (6): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (7): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (8): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (9): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (10): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (11): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 320, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(320, 1280, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(1280, 320, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #     )
    #     (5): PatchEmbed(
    #       (proj): Conv2d(320, 512, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1))
    #       (norm): Identity()
    #     )
    #     (6): Sequential(
    #       (0): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(2048, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (1): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(2048, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (2): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(2048, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #       (3): PoolFormerBlock(
    #         (norm1): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (token_mixer): Pooling(
    #           (pool): AvgPool2d(kernel_size=3, stride=1, padding=1)
    #         )
    #         (norm2): GroupNorm(1, 512, eps=1e-05, affine=True)
    #         (mlp): Mlp(
    #           (fc1): Conv2d(512, 2048, kernel_size=(1, 1), stride=(1, 1))
    #           (act): GELU()
    #           (fc2): Conv2d(2048, 512, kernel_size=(1, 1), stride=(1, 1))
    #           (drop): Dropout(p=0.0, inplace=False)
    #         )
    #         (drop_path): Identity()
    #       )
    #     )
    #   )
    #   (norm): GroupNorm(1, 512, eps=1e-05, affine=True)
    #   (head): Linear(in_features=512, out_features=1000, bias=True)
    # )

    return model


@register_model
def poolformer_s36(pretrained=False, **kwargs):
    """
    PoolFormer-S36 model, Params: 31M
    """
    layers = [6, 6, 18, 6]
    embed_dims = [64, 128, 320, 512]
    mlp_ratios = [4, 4, 4, 4]
    downsamples = [True, True, True, True]
    model = PoolFormer(
        layers, embed_dims=embed_dims, 
        mlp_ratios=mlp_ratios, downsamples=downsamples, 
        layer_scale_init_value=1e-6, 
        **kwargs)
    model.default_cfg = default_cfgs['poolformer_s']
    return model


@register_model
def poolformer_m36(pretrained=False, **kwargs):
    """
    PoolFormer-M36 model, Params: 56M
    """
    layers = [6, 6, 18, 6]
    embed_dims = [96, 192, 384, 768]
    mlp_ratios = [4, 4, 4, 4]
    downsamples = [True, True, True, True]
    model = PoolFormer(
        layers, embed_dims=embed_dims, 
        mlp_ratios=mlp_ratios, downsamples=downsamples, 
        layer_scale_init_value=1e-6, 
        **kwargs)
    model.default_cfg = default_cfgs['poolformer_m']
    return model


@register_model
def poolformer_m48(pretrained=False, **kwargs):
    """
    PoolFormer-M48 model, Params: 73M
    """
    layers = [8, 8, 24, 8]
    embed_dims = [96, 192, 384, 768]
    mlp_ratios = [4, 4, 4, 4]
    downsamples = [True, True, True, True]
    model = PoolFormer(
        layers, embed_dims=embed_dims, 
        mlp_ratios=mlp_ratios, downsamples=downsamples, 
        layer_scale_init_value=1e-6, 
        **kwargs)
    model.default_cfg = default_cfgs['poolformer_m']
    return model


if has_mmseg and has_mmdet:
    """
    The following models are for dense prediction based on 
    mmdetection and mmsegmentation
    """
    pdb.set_trace()

    @seg_BACKBONES.register_module()
    @det_BACKBONES.register_module()
    class poolformer_s12_feat(PoolFormer):
        """
        PoolFormer-S12 model, Params: 12M
        """
        def __init__(self, **kwargs):
            layers = [2, 2, 6, 2]
            embed_dims = [64, 128, 320, 512]
            mlp_ratios = [4, 4, 4, 4]
            downsamples = [True, True, True, True]
            super().__init__(
                layers, embed_dims=embed_dims, 
                mlp_ratios=mlp_ratios, downsamples=downsamples, 
                fork_feat=True,
                **kwargs)

    @seg_BACKBONES.register_module()
    @det_BACKBONES.register_module()
    class poolformer_s24_feat(PoolFormer):
        """
        PoolFormer-S24 model, Params: 21M
        """
        def __init__(self, **kwargs):
            layers = [4, 4, 12, 4]
            embed_dims = [64, 128, 320, 512]
            mlp_ratios = [4, 4, 4, 4]
            downsamples = [True, True, True, True]
            super().__init__(
                layers, embed_dims=embed_dims, 
                mlp_ratios=mlp_ratios, downsamples=downsamples, 
                fork_feat=True,
                **kwargs)

    @seg_BACKBONES.register_module()
    @det_BACKBONES.register_module()
    class poolformer_s36_feat(PoolFormer):
        """
        PoolFormer-S36 model, Params: 31M
        """
        def __init__(self, **kwargs):
            layers = [6, 6, 18, 6]
            embed_dims = [64, 128, 320, 512]
            mlp_ratios = [4, 4, 4, 4]
            downsamples = [True, True, True, True]
            super().__init__(
                layers, embed_dims=embed_dims, 
                mlp_ratios=mlp_ratios, downsamples=downsamples, 
                layer_scale_init_value=1e-6, 
                fork_feat=True,
                **kwargs)

    @seg_BACKBONES.register_module()
    @det_BACKBONES.register_module()
    class poolformer_m36_feat(PoolFormer):
        """
        PoolFormer-S36 model, Params: 56M
        """
        def __init__(self, **kwargs):
            layers = [6, 6, 18, 6]
            embed_dims = [96, 192, 384, 768]
            mlp_ratios = [4, 4, 4, 4]
            downsamples = [True, True, True, True]
            super().__init__(
                layers, embed_dims=embed_dims, 
                mlp_ratios=mlp_ratios, downsamples=downsamples, 
                layer_scale_init_value=1e-6, 
                fork_feat=True,
                **kwargs)

    @seg_BACKBONES.register_module()
    @det_BACKBONES.register_module()
    class poolformer_m48_feat(PoolFormer):
        """
        PoolFormer-M48 model, Params: 73M
        """
        def __init__(self, **kwargs):
            layers = [8, 8, 24, 8]
            embed_dims = [96, 192, 384, 768]
            mlp_ratios = [4, 4, 4, 4]
            downsamples = [True, True, True, True]
            super().__init__(
                layers, embed_dims=embed_dims, 
                mlp_ratios=mlp_ratios, downsamples=downsamples, 
                layer_scale_init_value=1e-6, 
                fork_feat=True,
                **kwargs)
