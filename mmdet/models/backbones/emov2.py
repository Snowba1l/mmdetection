import math
from functools import partial

import torch
from einops import rearrange, reduce, repeat
from torchvision.ops import DeformConv2d
import torch.nn as nn
import torch.nn.functional as F
from timm.layers.activations import *
from timm.layers import DropPath, trunc_normal_
from .emov2_basic_modules import get_norm, get_act, ConvNormAct, LayerScale2D
from torch.nn.modules.batchnorm import _BatchNorm
from mmdet.registry import MODELS

inplace = True


# ========== basic modules and ops ==========
def get_stem(dim_in, dim_mid):
    stem = nn.ModuleList([
        ConvNormAct(dim_in, dim_mid, kernel_size=3, stride=2, bias=True, norm_layer='bn_2d', act_layer='silu'),
        ConvNormAct(dim_mid, dim_mid, kernel_size=3, stride=1, groups=dim_mid, bias=False, norm_layer='bn_2d', act_layer='silu'),
        ConvNormAct(dim_mid, dim_mid, kernel_size=1, stride=1, bias=False, norm_layer='none', act_layer='none'),
    ])
    return stem


# --> conv
class Conv(nn.Module):

    def __init__(self, dim_in, dim_mid, kernel_size=1, groups=1, bias=False, norm_layer='bn_2d', act_layer='relu', inplace=True):
        super().__init__()
        self.net = ConvNormAct(dim_in, dim_mid, kernel_size=kernel_size, groups=groups, bias=bias, norm_layer=norm_layer,
                               act_layer=act_layer, inplace=inplace)

    def forward(self, x):
        return self.net(x)


# --> sa - remote
class EW_MHSA_Remote(nn.Module):

    def __init__(self, dim_in, dim_mid, norm_layer='bn_2d', act_layer='relu', dim_head=64, window_size=7,
                 qkv_bias=False, attn_drop=0., drop=0., drop_path=0., v_group=False, attn_pre=False, ls_value=1e-6):
        super().__init__()
        self.dim_head = dim_head
        self.window_size = window_size
        self.num_head = dim_in // dim_head
        self.scale = self.dim_head ** -0.5
        self.attn_pre = attn_pre
        self.qk = ConvNormAct(dim_in, int(dim_in * 2), kernel_size=1, bias=qkv_bias, norm_layer='none', act_layer='none')
        self.v = ConvNormAct(dim_in, dim_mid, kernel_size=1, groups=self.num_head if v_group else 1, bias=qkv_bias, norm_layer='none', act_layer=act_layer, inplace=inplace)
        self.attn_drop = nn.Dropout(attn_drop)

    def forward(self, x):
        B, C, H, W = x.shape
        # padding
        if self.window_size <= 0:
            window_size_W, window_size_H = W, H
        else:
            window_size_W, window_size_H = self.window_size, self.window_size
        pad_l, pad_t = 0, 0
        pad_r = (window_size_W - W % window_size_W) % window_size_W
        pad_b = (window_size_H - H % window_size_H) % window_size_H
        x = F.pad(x, (pad_l, pad_r, pad_t, pad_b, 0, 0,))
        n1, n2 = (H + pad_b) // window_size_H, (W + pad_r) // window_size_W
        x = rearrange(x, 'b c (h1 n1) (w1 n2) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()

        # attention
        b, c, h, w = x.shape
        qk = self.qk(x)
        qk = rearrange(qk, 'b (qk heads dim_head) h w -> qk b heads (h w) dim_head', qk=2, heads=self.num_head,
                       dim_head=self.dim_head).contiguous()
        q, k = qk[0], qk[1]
        attn_map = (q @ k.transpose(-2, -1)) * self.scale
        attn_map = attn_map.softmax(dim=-1)
        attn_map = self.attn_drop(attn_map)
        if self.attn_pre:
            x = rearrange(x, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa = attn_map @ x
            x_spa = rearrange(x_spa, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h,
                              w=w).contiguous()
            x_spa = self.v(x_spa)
        else:
            v = self.v(x)
            v = rearrange(v, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa = attn_map @ v
            x_spa = rearrange(x_spa, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h, w=w).contiguous()

        # unpadding
        x = rearrange(x_spa, '(b n1 n2) c h1 w1 -> b c (h1 n1) (w1 n2)', n1=n1, n2=n2).contiguous()
        if pad_r > 0 or pad_b > 0:
            x = x[:, :, :H, :W].contiguous()
        return x


# --> sa - close
class EW_MHSA_Close(nn.Module):

    def __init__(self, dim_in, dim_mid, norm_layer='bn_2d', act_layer='relu', dim_head=64, window_size=7,
                 qkv_bias=False, attn_drop=0., drop=0., drop_path=0., v_group=False, attn_pre=False, ls_value=1e-6):
        super().__init__()
        self.dim_head = dim_head
        self.window_size = window_size
        self.num_head = dim_in // dim_head
        self.scale = self.dim_head ** -0.5
        self.attn_pre = attn_pre
        self.qk = ConvNormAct(dim_in, int(dim_in * 2), kernel_size=1, bias=qkv_bias, norm_layer='none',
                              act_layer='none')
        self.v = ConvNormAct(dim_in, dim_mid, kernel_size=1, groups=self.num_head if v_group else 1, bias=qkv_bias,
                             norm_layer='none', act_layer=act_layer, inplace=inplace)
        self.attn_drop = nn.Dropout(attn_drop)

    def forward(self, x):
        B, C, H, W = x.shape
        # padding
        if self.window_size <= 0:
            window_size_W, window_size_H = W, H
        else:
            window_size_W, window_size_H = self.window_size, self.window_size
        pad_l, pad_t = 0, 0
        pad_r = (window_size_W - W % window_size_W) % window_size_W
        pad_b = (window_size_H - H % window_size_H) % window_size_H
        x = F.pad(x, (pad_l, pad_r, pad_t, pad_b, 0, 0,))
        n1, n2 = (H + pad_b) // window_size_H, (W + pad_r) // window_size_W
        # x = rearrange(x, 'b c (h1 n1) (w1 n2) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()
        x = rearrange(x, 'b c (n1 h1) (n2 w1) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()

        # attention
        b, c, h, w = x.shape
        qk = self.qk(x)
        qk = rearrange(qk, 'b (qk heads dim_head) h w -> qk b heads (h w) dim_head', qk=2, heads=self.num_head,
                       dim_head=self.dim_head).contiguous()
        q, k = qk[0], qk[1]
        attn_map = (q @ k.transpose(-2, -1)) * self.scale
        attn_map = attn_map.softmax(dim=-1)
        attn_map = self.attn_drop(attn_map)
        if self.attn_pre:
            x = rearrange(x, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa = attn_map @ x
            x_spa = rearrange(x_spa, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h,
                              w=w).contiguous()
            x_spa = self.v(x_spa)
        else:
            v = self.v(x)
            v = rearrange(v, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa = attn_map @ v
            x_spa = rearrange(x_spa, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h,
                              w=w).contiguous()

        # unpadding
        # x = rearrange(x_spa, '(b n1 n2) c h1 w1 -> b c (h1 n1) (w1 n2)', n1=n1, n2=n2).contiguous()
        x = rearrange(x_spa, '(b n1 n2) c h1 w1 -> b c (n1 h1) (n2 w1)', n1=n1, n2=n2).contiguous()
        if pad_r > 0 or pad_b > 0:
            x = x[:, :, :H, :W].contiguous()
        return x


class EW_MHSA_Hybrid(nn.Module):

    def __init__(self, dim_in, dim_mid, norm_layer='bn_2d', act_layer='relu', dim_head=64, window_size=7,
                 qkv_bias=False, attn_drop=0., drop=0., drop_path=0., v_group=False, attn_pre=False, ls_value=1e-6):
        super().__init__()
        self.dim_head = dim_head
        self.window_size = window_size
        self.num_head = dim_in // dim_head
        self.scale = self.dim_head ** -0.5
        self.attn_pre = attn_pre
        self.qk = ConvNormAct(dim_in, int(dim_in * 2), kernel_size=1, bias=qkv_bias, norm_layer='none',
                              act_layer='none')
        self.v = ConvNormAct(dim_in, dim_mid, kernel_size=1, groups=self.num_head if v_group else 1, bias=qkv_bias,
                             norm_layer='none', act_layer=act_layer, inplace=inplace)
        self.attn_drop = nn.Dropout(attn_drop)

    def forward(self, x):
        B, C, H, W = x.shape
        # padding
        if self.window_size <= 0:
            window_size_W, window_size_H = W, H
        else:
            window_size_W, window_size_H = self.window_size, self.window_size
        pad_l, pad_t = 0, 0
        pad_r = (window_size_W - W % window_size_W) % window_size_W
        pad_b = (window_size_H - H % window_size_H) % window_size_H
        x = F.pad(x, (pad_l, pad_r, pad_t, pad_b, 0, 0,))
        n1, n2 = (H + pad_b) // window_size_H, (W + pad_r) // window_size_W

        x_remote = rearrange(x, 'b c (h1 n1) (w1 n2) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()
        x_close = rearrange(x, 'b c (n1 h1) (n2 w1) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()

        qk = self.qk(x)
        qk_remote = rearrange(qk, 'b c (h1 n1) (w1 n2) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()
        qk_close = rearrange(qk, 'b c (n1 h1) (n2 w1) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()

        # ==> attention
        b, c, h, w = x_remote.shape

        qk_remote = rearrange(qk_remote, 'b (qk heads dim_head) h w -> qk b heads (h w) dim_head', qk=2, heads=self.num_head, dim_head=self.dim_head).contiguous()
        qk_close = rearrange(qk_close, 'b (qk heads dim_head) h w -> qk b heads (h w) dim_head', qk=2, heads=self.num_head, dim_head=self.dim_head).contiguous()

        attn_map_remote = (qk_remote[0] @ qk_remote[1].transpose(-2, -1)) * self.scale
        attn_map_remote = attn_map_remote.softmax(dim=-1)
        attn_map_remote = self.attn_drop(attn_map_remote)
        attn_map_close = (qk_close[0] @ qk_close[1].transpose(-2, -1)) * self.scale
        attn_map_close = attn_map_close.softmax(dim=-1)
        attn_map_close = self.attn_drop(attn_map_close)

        if self.attn_pre:
            x_remote = rearrange(x_remote, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa_remote = attn_map_remote @ x_remote
            x_spa_remote = rearrange(x_spa_remote, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h, w=w).contiguous()
            x_spa_remote = rearrange(x_spa_remote, '(b n1 n2) c h1 w1 -> b c (h1 n1) (w1 n2)', n1=n1, n2=n2).contiguous()

            x_close = rearrange(x_close, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa_close = attn_map_close @ x_close
            x_spa_close = rearrange(x_spa_close, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h, w=w).contiguous()
            x_spa_close = rearrange(x_spa_close, '(b n1 n2) c h1 w1 -> b c (h1 n1) (w1 n2)', n1=n1, n2=n2).contiguous()

            x_spa = x_spa_remote + x_spa_close
            x_spa = self.v(x_spa)
        else:
            v = self.v(x)
            v_remote = rearrange(v, 'b c (h1 n1) (w1 n2) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()
            v_close = rearrange(v, 'b c (n1 h1) (n2 w1) -> (b n1 n2) c h1 w1', n1=n1, n2=n2).contiguous()

            v_remote = rearrange(v_remote, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa_remote = attn_map_remote @ v_remote
            x_spa_remote = rearrange(x_spa_remote, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h, w=w).contiguous()
            x_spa_remote = rearrange(x_spa_remote, '(b n1 n2) c h1 w1 -> b c (h1 n1) (w1 n2)', n1=n1, n2=n2).contiguous()

            v_close = rearrange(v_close, 'b (heads dim_head) h w -> b heads (h w) dim_head', heads=self.num_head).contiguous()
            x_spa_close = attn_map_close @ v_close
            x_spa_close = rearrange(x_spa_close, 'b heads (h w) dim_head -> b (heads dim_head) h w', heads=self.num_head, h=h, w=w).contiguous()
            x_spa_close = rearrange(x_spa_close, '(b n1 n2) c h1 w1 -> b c (n1 h1) (n2 w1)', n1=n1, n2=n2).contiguous()
            x_spa = x_spa_remote + x_spa_close

        # unpadding
        if pad_r > 0 or pad_b > 0:
            x_spa = x_spa[:, :, :H, :W].contiguous()
        return x_spa

class iiRMB(nn.Module):

    def __init__(self, dim_in, dim_out, norm_in=True, has_skip=True, exp_ratio=1.0, norm_layer='bn_2d',
                 act_layer='relu', dw_ks=3, stride=1, dim_head=64, window_size=7, hybrid_eops=[0], conv_ks=1, conv_groups=1, qkv_bias=False,
                 attn_drop=0., drop=0., drop_path=0., v_group=False, attn_pre=False, ls_value=1e-6):
        super().__init__()
        self.norm = get_norm(norm_layer)(dim_in) if norm_in else nn.Identity()
        dim_mid = int(dim_in * exp_ratio)

        self.has_skip = (dim_in == dim_out and stride == 1) and has_skip
        self.hybrid_eops = hybrid_eops
        eops = []
        for eop_idx in self.hybrid_eops:
            if eop_idx == 0:
                eop = Conv(dim_in, dim_mid, kernel_size=conv_ks, groups=conv_groups, bias=qkv_bias, norm_layer='none', act_layer=act_layer, inplace=inplace)
            elif eop_idx == 1:
                eop = EW_MHSA_Remote(dim_in, dim_mid, norm_layer=norm_layer, act_layer=act_layer, dim_head=dim_head, window_size=window_size,
                                     qkv_bias=qkv_bias, attn_drop=attn_drop, drop=drop, drop_path=drop_path, v_group=v_group, attn_pre=attn_pre, ls_value=ls_value)
            elif eop_idx == 2:
                eop = EW_MHSA_Close(dim_in, dim_mid, norm_layer=norm_layer, act_layer=act_layer, dim_head=dim_head, window_size=window_size,
                                    qkv_bias=qkv_bias, attn_drop=attn_drop, drop=drop, drop_path=drop_path, v_group=v_group, attn_pre=attn_pre, ls_value=ls_value)
            elif eop_idx == 3:
                eop = EW_MHSA_Hybrid(dim_in, dim_mid, norm_layer=norm_layer, act_layer=act_layer, dim_head=dim_head, window_size=window_size,
                                     qkv_bias=qkv_bias, attn_drop=attn_drop, drop=drop, drop_path=drop_path, v_group=v_group, attn_pre=attn_pre, ls_value=ls_value)
            else:
                eop = None
            if eop:
                eops.append(eop)
        self.eops = nn.ModuleList(eops)
        if dw_ks > 0:
            self.conv_local = ConvNormAct(dim_mid, dim_mid, kernel_size=dw_ks, stride=stride, groups=dim_mid, norm_layer='bn_2d', act_layer='silu', inplace=inplace)
        else:
            self.conv_local = nn.Identity()
        self.proj_drop = nn.Dropout(drop)
        self.proj = ConvNormAct(dim_mid, dim_out, kernel_size=1, norm_layer='none', act_layer='none', inplace=inplace)
        self.ls = LayerScale2D(dim_out, init_values=ls_value) if ls_value > 0 else nn.Identity()
        self.drop_path = DropPath(drop_path) if drop_path else nn.Identity()

    def forward(self, x):
        shortcut = x
        x = self.norm(x)

        xs = []
        for eop in self.eops:
            xs.append(eop(x))
        x = sum(xs) if len(self.eops) > 1 else xs[0]

        x_l = self.conv_local(x)
        x = (x + x_l) if self.has_skip else x_l

        x = self.proj_drop(x)
        x = self.proj(x)

        x = (shortcut + self.drop_path(self.ls(x))) if self.has_skip else self.ls(x)
        return x


@MODELS.register_module()
class EMO2(nn.Module):

    def __init__(self,
                 dim_in=3, num_classes=1000, img_size=224,
                 depths=[1, 2, 4, 2],
                 embed_dims=[64, 128, 256, 512],
                 exp_ratios=[4., 4., 4., 4.],
                 norm_layers=['bn_2d', 'bn_2d', 'ln_2d', 'ln_2d'],
                 act_layers=['silu', 'silu', 'gelu', 'gelu'],
                 dw_kss=[3, 3, 5, 5],
                 dim_heads=[32, 32, 32, 32],
                 window_sizes=[7, 7, 7, 7],
                 hybrid_eopss=[[0], [0], [1], [1]],
                 conv_kss=[1, 1, 1, 1],
                 conv_groupss=[1, 1, 1, 1],
                 qkv_bias=True, attn_drop=0., drop=0., drop_path=0.,
                 v_group=False, attn_pre=False, ls_value=1e-6,
                 sync_bn=False, out_indices=(1, 2, 3, 4), pretrained=None, frozen_stages=-1, norm_eval=False):
        super().__init__()
        self.sync_bn = sync_bn
        self.out_indices = out_indices
        self.pretrained = pretrained
        self.frozen_stages = frozen_stages
        self.norm_eval = norm_eval

        self.num_classes = num_classes
        assert num_classes > 0
        dprs = [x.item() for x in torch.linspace(0, drop_path, sum(depths))]
        emb_dim_pre = embed_dims[0] // 2
        self.stage0 = get_stem(dim_in, emb_dim_pre)
        fea_size = img_size // 2
        for i in range(len(depths)):
            fea_size = fea_size // 2
            layers = []
            dpr = dprs[sum(depths[:i]):sum(depths[:i + 1])]
            for j in range(depths[i]):
                if j == 0:
                    stride, has_skip, hybrid_eops, exp_ratio, conv_ks, conv_groups = 2, False, [0], exp_ratios[
                                                                                                        i] * 2, 1, 1
                    dw_ks = dw_kss[i] if dw_kss[i] > 0 else 5
                else:
                    stride, has_skip, hybrid_eops, exp_ratio, conv_ks, conv_groups = 1, True, hybrid_eopss[i], \
                        exp_ratios[i], conv_kss[i], conv_groupss[i]
                    dw_ks = dw_kss[i]
                layers.append(iiRMB(
                    emb_dim_pre, embed_dims[i], norm_in=True, has_skip=has_skip, exp_ratio=exp_ratio,
                    norm_layer=norm_layers[i], act_layer=act_layers[i], dw_ks=dw_ks,
                    stride=stride, dim_head=dim_heads[i], window_size=window_sizes[i], hybrid_eops=hybrid_eops,
                    conv_ks=conv_ks, conv_groups=conv_groups, qkv_bias=qkv_bias, attn_drop=attn_drop, drop=drop,
                    drop_path=dpr[j], v_group=v_group,
                    attn_pre=attn_pre, ls_value=ls_value
                ))
                emb_dim_pre = embed_dims[i]
            self.__setattr__(f'stage{i + 1}', nn.ModuleList(layers))

        self._init_weights()
        self._sync_bn() if sync_bn else None
        self._freeze_stages()

    def _init_weights(self):
        if self.pretrained is None:
            for m in self.parameters():
                if isinstance(m, nn.Linear):
                    trunc_normal_(m.weight, std=.02)
                    if isinstance(m, nn.Linear) and m.bias is not None:
                        nn.init.constant_(m.bias, 0)
                elif isinstance(m, nn.LayerNorm):
                    nn.init.constant_(m.bias, 0)
                    nn.init.constant_(m.weight, 1.0)
        else:
            state_dict = torch.load(self.pretrained, map_location='cpu')
            self_state_dict = self.state_dict()
            for k, v in state_dict.items():
                if k in self_state_dict.keys():
                    self_state_dict.update({k: v})
            self.load_state_dict(self_state_dict, strict=True)
            print(f'load ckpt from {self.pretrained}')

    def _sync_bn(self):
        self.stage0 = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.stage0)
        self.stage1 = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.stage1)
        self.stage2 = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.stage2)
        self.stage3 = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.stage3)
        self.stage4 = torch.nn.SyncBatchNorm.convert_sync_batchnorm(self.stage4)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'token'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'alpha', 'gamma', 'beta'}

    @torch.jit.ignore
    def no_ft_keywords(self):
        # return {'head.weight', 'head.bias'}
        return {}

    @torch.jit.ignore
    def ft_head_keywords(self):
        return {'head.weight', 'head.bias'}, self.num_classes

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes):
        self.num_classes = num_classes
        self.head = nn.Linear(self.pre_dim, num_classes) if num_classes > 0 else nn.Identity()

    def check_bn(self):
        for name, m in self.named_modules():
            if isinstance(m, nn.modules.batchnorm._NormBase):
                m.running_mean = torch.nan_to_num(m.running_mean, nan=0, posinf=1, neginf=-1)
                m.running_var = torch.nan_to_num(m.running_var, nan=0, posinf=1, neginf=-1)

    # m.running_mean.nan_to_num_(nan=0, posinf=1, neginf=-1)
    # m.running_var.nan_to_num_(nan=0, posinf=1, neginf=-1)

    def forward(self, x):
        out = []
        for blk in self.stage0:
            x = blk(x)
        out.append(x)
        for blk in self.stage1:
            x = blk(x)
        out.append(x)
        for blk in self.stage2:
            x = blk(x)
        out.append(x)
        for blk in self.stage3:
            x = blk(x)
        out.append(x)
        for blk in self.stage4:
            x = blk(x)
        out.append(x)
        out = tuple([out[i] for i in self.out_indices])
        return out

    def _freeze_stages(self):
        for i in range(0, self.frozen_stages + 1):
            m = getattr(self, f'stage{i}')
            m.eval()
            for param in m.parameters():
                param.requires_grad = False

    def train(self, mode=True):
        """Convert the model into training mode while keep normalization layer
        freezed."""
        super(EMO2, self).train(mode)
        self._freeze_stages()
        if mode and self.norm_eval:
            for m in self.modules():
                # trick: eval have effect on BatchNorm only
                if isinstance(m, _BatchNorm):
                    m.eval()
