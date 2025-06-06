import torch
import torch.nn as nn
# import numpy as np
# import math
# from timm.models.vision_transformer import PatchEmbed, Attention, Mlp

from .dit_models import TimestepEmbedder, LabelEmbedder, DiTBlock, get_2d_sincos_pos_embed


class DiTwoEmbedder(nn.Module):
    """
    Diffusion model with a Transformer backbone, performing directly on the ViT token latents rather than spatial latents.
    """

    def __init__(
        self,
        input_size=224,  # raw img input size
        # patch_size=14, # dino version
        in_channels=4,
        hidden_size=1152,
        depth=28,
        num_heads=16,
        mlp_ratio=4.0,
        class_dropout_prob=0.1,
        num_classes=1000,
        learn_sigma=True,
    ):
        super().__init__()
        self.learn_sigma = learn_sigma
        self.in_channels = in_channels
        self.out_channels = in_channels * 2 if learn_sigma else in_channels
        self.patch_size = 14  # dino-v2 patch sized fixed in this project
        self.num_heads = num_heads

        self.t_embedder = TimestepEmbedder(hidden_size)
        if num_classes > 0:
            self.y_embedder = LabelEmbedder(num_classes, hidden_size,
                                            class_dropout_prob)
        else:
            self.y_embedder = None

        self.num_patches = (input_size // self.patch_size)**2

        # Will use fixed sin-cos embedding:
        self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches,
                                                  hidden_size),
                                      requires_grad=False)

        self.blocks = nn.ModuleList([
            DiTBlock(hidden_size, num_heads, mlp_ratio=mlp_ratio)
            for _ in range(depth)
        ])
        self.initialize_weights()

    def initialize_weights(self):
        # Initialize transformer layers:
        def _basic_init(module):
            if isinstance(module, nn.Linear):
                torch.nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)

        self.apply(_basic_init)

        # Initialize (and freeze) pos_embed by sin-cos embedding:
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1],
                                            int(self.num_patches**0.5))
        # st()
        self.pos_embed.data.copy_(
            torch.from_numpy(pos_embed).float().unsqueeze(0))


        # Initialize label embedding table:
        if self.y_embedder is not None:
            nn.init.normal_(self.y_embedder.embedding_table.weight, std=0.02)

        # Initialize timestep embedding MLP:
        nn.init.normal_(self.t_embedder.mlp[0].weight, std=0.02)
        nn.init.normal_(self.t_embedder.mlp[2].weight, std=0.02)

        # Zero-out adaLN modulation layers in DiT blocks:
        for block in self.blocks:
            nn.init.constant_(block.adaLN_modulation[-1].weight, 0)
            nn.init.constant_(block.adaLN_modulation[-1].bias, 0)

    def forward(self, x, t, y=None):
        """
        Forward pass of DiT.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        y: (N,) tensor of class labels
        """

        x = x + self.pos_embed

        t = self.t_embedder(t)  # (N, D)

        if self.y_embedder is not None:
            assert y is not None
            y = self.y_embedder(y, self.training)  # (N, D)
            c = t + y  # (N, D)
        else:
            c = t

        for block in self.blocks:
            x = block(x, c)  # (N, T, D)


        return x

    def forward_with_cfg(self, x, t, y, cfg_scale):
        """
        Forward pass of DiT, but also batches the unconditional forward pass for classifier-free guidance.
        """
        # https://github.com/openai/glide-text2im/blob/main/notebooks/text2im.ipynb
        half = x[:len(x) // 2]
        combined = torch.cat([half, half], dim=0)
        model_out = self.forward(combined, t, y)
        eps, rest = model_out[:, :3], model_out[:, 3:]
        cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
        half_eps = uncond_eps + cfg_scale * (cond_eps - uncond_eps)
        eps = torch.cat([half_eps, half_eps], dim=0)
        return torch.cat([eps, rest], dim=1)

    def forward_with_cfg_unconditional(self, x, t, y=None, cfg_scale=None):
        """
        Forward pass of DiT, but also batches the unconditional forward pass for classifier-free guidance.
        """

        combined = x
        model_out = self.forward(combined, t, y)

        return model_out


class DiTwoEmbedderLongSkipConnection(nn.Module):

    def __init__(
        self,
        input_size=224,  # raw img input size
        patch_size=14,  # dino version
        in_channels=4,
        hidden_size=1152,
        depth=28,
        num_heads=16,
        mlp_ratio=4.0,
        class_dropout_prob=0.1,
        num_classes=1000,
        learn_sigma=True,
    ):
        """DiT with long skip-connections from U-ViT, CVPR 23'
        """
        super().__init__()
        self.learn_sigma = learn_sigma
        self.in_channels = in_channels
        self.out_channels = in_channels * 2 if learn_sigma else in_channels
        self.patch_size = patch_size
        self.num_heads = num_heads

        self.t_embedder = TimestepEmbedder(hidden_size)
        if num_classes > 0:
            self.y_embedder = LabelEmbedder(num_classes, hidden_size,
                                            class_dropout_prob)
        else:
            self.y_embedder = None

        self.num_patches = (input_size // patch_size)**2

        # Will use fixed sin-cos embedding:
        self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches,
                                                  hidden_size),
                                      requires_grad=False)

        self.blocks = nn.ModuleList([
            DiTBlock(hidden_size, num_heads, mlp_ratio=mlp_ratio)
            for _ in range(depth)
        ])

        # ! add long-skip-connections from U-ViT
        self.in_blocks = nn.ModuleList([
            DiTBlock(hidden_size, num_heads, mlp_ratio=mlp_ratio)
            for _ in range(depth // 2)
        ])

        self.mid_block = DiTBlock(hidden_size, num_heads, mlp_ratio=mlp_ratio)

        self.out_blocks = nn.ModuleList([
            DiTBlock(hidden_size, num_heads, mlp_ratio=mlp_ratio)
            for _ in range(depth // 2)
        ])

        self.initialize_weights()

    def initialize_weights(self):
        # Initialize transformer layers:
        def _basic_init(module):
            if isinstance(module, nn.Linear):
                torch.nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)

        self.apply(_basic_init)

        # Initialize (and freeze) pos_embed by sin-cos embedding:
        pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1],
                                            int(self.num_patches**0.5))
        # st()
        self.pos_embed.data.copy_(
            torch.from_numpy(pos_embed).float().unsqueeze(0))

        # Initialize label embedding table:
        if self.y_embedder is not None:
            nn.init.normal_(self.y_embedder.embedding_table.weight, std=0.02)

        # Initialize timestep embedding MLP:
        nn.init.normal_(self.t_embedder.mlp[0].weight, std=0.02)
        nn.init.normal_(self.t_embedder.mlp[2].weight, std=0.02)

        # Zero-out adaLN modulation layers in DiT blocks:
        for block in self.blocks:
            nn.init.constant_(block.adaLN_modulation[-1].weight, 0)
            nn.init.constant_(block.adaLN_modulation[-1].bias, 0)


    def forward(self, x, t, y=None):
        """
        Forward pass of DiT.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        y: (N,) tensor of class labels
        """

        # ! no embedder operation
        x = x + self.pos_embed

        t = self.t_embedder(t)  # (N, D)

        if self.y_embedder is not None:
            assert y is not None
            y = self.y_embedder(y, self.training)  # (N, D)
            c = t + y  # (N, D)
        else:
            c = t


        skips = []
        for blk in self.in_blocks:
            x = blk(x)
            skips.append(x)

        x = self.mid_block(x)

        for blk in self.out_blocks:
            x = blk(x, skips.pop())


        return x

    def forward_with_cfg(self, x, t, y, cfg_scale):
        """
        Forward pass of DiT, but also batches the unconditional forward pass for classifier-free guidance.
        """
        # https://github.com/openai/glide-text2im/blob/main/notebooks/text2im.ipynb
        half = x[:len(x) // 2]
        combined = torch.cat([half, half], dim=0)
        model_out = self.forward(combined, t, y)

        eps, rest = model_out[:, :3], model_out[:, 3:]
        cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
        half_eps = uncond_eps + cfg_scale * (cond_eps - uncond_eps)
        eps = torch.cat([half_eps, half_eps], dim=0)
        return torch.cat([eps, rest], dim=1)

    def forward_with_cfg_unconditional(self, x, t, y=None, cfg_scale=None):
        """
        Forward pass of DiT, but also batches the unconditional forward pass for classifier-free guidance.
        """

        combined = x
        model_out = self.forward(combined, t, y)

        return model_out


#################################################################################
#                                   DiT Configs                                  #
#################################################################################


def DiT_woembed_S(**kwargs):
    return DiTwoEmbedder(depth=12, hidden_size=384, num_heads=6, **kwargs)


def DiT_woembed_B(**kwargs):
    return DiTwoEmbedder(depth=12, hidden_size=768, num_heads=12, **kwargs)


def DiT_woembed_L(**kwargs):
    return DiTwoEmbedder(
        depth=24,
        hidden_size=1024,
        num_heads=16,
        **kwargs)


DiT_woembed_models = {
    'DiT-wo-S': DiT_woembed_S,
    'DiT-wo-B': DiT_woembed_B,
    'DiT-wo-L': DiT_woembed_L,
}
