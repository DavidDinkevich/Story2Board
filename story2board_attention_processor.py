import cv2
import numpy as np

from skimage.morphology import remove_small_objects
from skimage.filters import threshold_otsu

from typing import Optional
import torch
import torch.nn.functional as F

from diffusers.models.attention_processor import Attention

class Story2BoardAttentionProcessor:
    """Story2Board attention processor that implements Reciprocal Attention Value Mixing"""

    def __init__(self):
        if not hasattr(F, "scaled_dot_product_attention"):
            raise ImportError("FluxAttnProcessor2_0 requires PyTorch 2.0, to use it, please upgrade PyTorch to 2.0.")
        
    def __call__(
        self,
        attn: Attention,
        hidden_states: torch.FloatTensor,
        encoder_hidden_states: torch.FloatTensor = None,
        attention_mask: Optional[torch.FloatTensor] = None,
        image_rotary_emb: Optional[torch.Tensor] = None,
        attn_store=None,
        n_prompt_tokens=None,
        n_image_tokens=None,
        ravm_mixing_coef=None,
        first_mixing_block=None,
        last_mixing_block=None,
        first_mixing_denoising_step=None,
        last_mixing_denoising_step=None,
    ) -> torch.FloatTensor:
        batch_size, _, _ = hidden_states.shape if encoder_hidden_states is None else encoder_hidden_states.shape
        
        # `sample` projections.
        query = attn.to_q(hidden_states)
        key = attn.to_k(hidden_states)
        value = attn.to_v(hidden_states)

        inner_dim = key.shape[-1]
        head_dim = inner_dim // attn.heads

        query = query.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        key = key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        value = value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        if attn.norm_q is not None:
            query = attn.norm_q(query)
        if attn.norm_k is not None:
            key = attn.norm_k(key)

        # the attention in FluxSingleTransformerBlock does not use `encoder_hidden_states`
        if encoder_hidden_states is not None:
            # `context` projections.
            encoder_hidden_states_query_proj = attn.add_q_proj(encoder_hidden_states)
            encoder_hidden_states_key_proj = attn.add_k_proj(encoder_hidden_states)
            encoder_hidden_states_value_proj = attn.add_v_proj(encoder_hidden_states)

            encoder_hidden_states_query_proj = encoder_hidden_states_query_proj.view(
                batch_size, -1, attn.heads, head_dim
            ).transpose(1, 2)
            encoder_hidden_states_key_proj = encoder_hidden_states_key_proj.view(
                batch_size, -1, attn.heads, head_dim
            ).transpose(1, 2)
            encoder_hidden_states_value_proj = encoder_hidden_states_value_proj.view(
                batch_size, -1, attn.heads, head_dim
            ).transpose(1, 2)

            if attn.norm_added_q is not None:
                encoder_hidden_states_query_proj = attn.norm_added_q(encoder_hidden_states_query_proj)
            if attn.norm_added_k is not None:
                encoder_hidden_states_key_proj = attn.norm_added_k(encoder_hidden_states_key_proj)

            # attention
            query = torch.cat([encoder_hidden_states_query_proj, query], dim=2)
            key = torch.cat([encoder_hidden_states_key_proj, key], dim=2)
            value = torch.cat([encoder_hidden_states_value_proj, value], dim=2)
                
        n_panel_tokens = n_image_tokens//2

        attn_scores = torch.stack([attn.get_attention_scores(query=query[i], key=key[i]).mean(dim=0) for i in range(batch_size)])
        attn_to_top = attn_scores[:, n_prompt_tokens + n_panel_tokens:, n_prompt_tokens:n_prompt_tokens + n_panel_tokens]
        attn_to_bottom = attn_scores[:, n_prompt_tokens:n_prompt_tokens+n_panel_tokens, n_prompt_tokens+n_panel_tokens:]

        mut_attn_tb = torch.minimum(attn_to_bottom, attn_to_top.transpose(-1, -2)) # (bsz, 2048, 2048)
        mut_attn_bt = torch.minimum(attn_to_top, attn_to_bottom.transpose(-1, -2)) # (bsz, 2048, 2048)
        mut_attn = torch.cat([mut_attn_tb, mut_attn_bt], dim=1)
                            
        attn_store.store_attention_map(mut_attn)

        if first_mixing_denoising_step <= attn_store._get_curr_diffusion_step() <= last_mixing_denoising_step:                        
            mut_attn = attn_store.aggregate_attn_maps() # (bsz, 2048, 2048)
            mut_attn_tb = mut_attn[:, :n_panel_tokens, :]
            mut_attn_bt = mut_attn[:, n_panel_tokens:, :]

            for i in range(batch_size):
                # MUTUAL ATTENTION                
                top = mut_attn_tb[i, :, :].max(dim=-1).values
                bottom = mut_attn_bt[i, :, :].max(dim=-1).values

                # Convert to numpy so that we can use OpenCV
                top = top.to(torch.float32).cpu().numpy()
                bottom = bottom.to(torch.float32).cpu().numpy()
                bottom_labels = self.get_cc_watershed(bottom.reshape(32, 64))

                # Back to GPU-acceleration
                bottom_labels = torch.from_numpy(bottom_labels).to(device=hidden_states.device, dtype=torch.uint8)

                if first_mixing_block <= attn_store._get_curr_trans_block() <= last_mixing_block:
                    # Get flattened token indices
                    bottom_flat = bottom_labels.flatten()
                    bottom_indices = torch.where(bottom_flat > 0)[0]
                    # Slice mutual attention (B x T): [bottom_indices, top_indices]
                    attention_scores = mut_attn_bt[i, :, :].index_select(0, bottom_indices)  # shape: (k, 2048)
                    # For each bottom token, find top token it attends to most
                    top_matches = attention_scores.argmax(dim=-1)  # (k,)
                    # Map matched top indices
                    matched_top_indices = top_matches  # (k,)
                    # Gather value embeddings
                    # save: (H, k, D)
                    save = value[i, :, n_prompt_tokens + n_panel_tokens + bottom_indices, :]  # (H, k, D)
                    paste = value[i, :, n_prompt_tokens + matched_top_indices, :]  # (H, k, D)
                    # Blend and assign
                    blended = (1 - ravm_mixing_coef) * save + ravm_mixing_coef * paste
                    value[i, :, n_prompt_tokens + n_panel_tokens + bottom_indices, :] = blended
               
        if image_rotary_emb is not None:
            from diffusers.models.embeddings import apply_rotary_emb

            query = apply_rotary_emb(query, image_rotary_emb)
            key = apply_rotary_emb(key, image_rotary_emb)        

        hidden_states = F.scaled_dot_product_attention(query, key, value, dropout_p=0.0, is_causal=False)

        hidden_states = hidden_states.transpose(1, 2).reshape(batch_size, -1, attn.heads * head_dim)
        hidden_states = hidden_states.to(query.dtype)
       
        if encoder_hidden_states is not None:
            encoder_hidden_states, hidden_states = (
                hidden_states[:, : encoder_hidden_states.shape[1]],
                hidden_states[:, encoder_hidden_states.shape[1] :],
            )

            # linear proj
            hidden_states = attn.to_out[0](hidden_states)
            # dropout
            hidden_states = attn.to_out[1](hidden_states)

            encoder_hidden_states = attn.to_add_out(encoder_hidden_states)

            return hidden_states, encoder_hidden_states
        else:
            return hidden_states

    def otzu_binarize(self, image):
        # Normalize image between 0 and 1
        image = (image - image.min()) / (image.max() - image.min())
        # Threshold the image to separate clumps
        thresh = threshold_otsu(image)
        _, binary = cv2.threshold(image, thresh, 1, cv2.THRESH_BINARY)
        binary = (binary * 255).astype(np.bool)
        return binary, thresh

    def get_cc_watershed(self, image, min_size=9):
        """
        Watershed + remove small components (default: removes clusters smaller than 3x3).
        
        Args:
            image (np.ndarray): Input binary or grayscale image
            min_size (int): Minimum number of pixels per component to keep
        
        Returns:
            labels (np.ndarray): Labeled connected components after watershed
        """
        # We ignore a small margin on the borders of the image, as for Flux it usually is
        # decorative and does not contain image content. Without this, Reciprocal Attention can
        # be more noisy.
        margin = 3
        image[:margin, :] = 0
        image[-margin:, :] = 0
        image[:, :margin] = 0
        image[:, -margin:] = 0
        
        # Step 1: Otsu Binarization
        binary, _ = self.otzu_binarize(image)
        
        binary = remove_small_objects(binary, min_size=min_size)
        return binary

