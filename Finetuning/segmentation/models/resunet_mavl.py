import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
import torch
from torchvision.models.feature_extraction import get_graph_node_names
from torchvision.models.feature_extraction import create_feature_extractor
import torchvision

class ConvBlock(nn.Module):
    """
    Based on https://github.com/kevinlu1211/pytorch-unet-resnet-50-encoder/blob/master/u_net_resnet_50_encoder.py

    Helper module that consists of a Conv -> BN -> ReLU
    """

    def __init__(self, in_channels, out_channels, padding=1, kernel_size=3, stride=1, with_nonlinearity=True):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, padding=padding, kernel_size=kernel_size, stride=stride)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True) if with_nonlinearity else None

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        if self.relu is not None:
            x = self.relu(x)
        return x


class Bridge(nn.Module):
    """
    Based on https://github.com/kevinlu1211/pytorch-unet-resnet-50-encoder/blob/master/u_net_resnet_50_encoder.py
    """

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.bridge = nn.Sequential(
            ConvBlock(in_channels, out_channels),
            ConvBlock(out_channels, out_channels)
        )

    def forward(self, x):
        return self.bridge(x)


class UpBlockForUNetWithResNet50(nn.Module):
    """
    Based on https://github.com/kevinlu1211/pytorch-unet-resnet-50-encoder/blob/master/u_net_resnet_50_encoder.py

    Up block that encapsulates one up-sampling step which consists of Upsample -> ConvBlock -> ConvBlock
    """

    def __init__(self, in_channels, out_channels, up_conv_in_channels=None, up_conv_out_channels=None,
                 upsampling_method="conv_transpose"):
        super().__init__()

        if up_conv_in_channels == None:
            up_conv_in_channels = in_channels
        if up_conv_out_channels == None:
            up_conv_out_channels = out_channels

        if upsampling_method == "conv_transpose":
            self.upsample = nn.ConvTranspose2d(up_conv_in_channels, up_conv_out_channels, kernel_size=2, stride=2)
        elif upsampling_method == "bilinear":
            self.upsample = nn.Sequential(
                nn.Upsample(mode='bilinear', scale_factor=2),
                nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1)
            )
        self.conv_block_1 = ConvBlock(in_channels, out_channels)
        self.conv_block_2 = ConvBlock(out_channels, out_channels)

    def forward(self, up_x, down_x):
        """
        :param up_x: this is the output from the previous up block
        :param down_x: this is the output from the down block
        :return: upsampled feature map
        """
        x = self.upsample(up_x)
        x = torch.cat([x, down_x], 1)
        x = self.conv_block_1(x)
        x = self.conv_block_2(x)
        return x

class ModelResUNet_MAVL_ft(nn.Module):
    def __init__(self, base_model,out_size, imagenet_pretrain=True,linear_probe=False):
        super(ModelResUNet_MAVL_ft, self).__init__()
        self.model_name = base_model
        self.backbone, num_ftrs = self._get_basemodel(base_model, pretrained=imagenet_pretrain)
        self.d = {
            'input': 3,
            'conv1': 64,
            'conv2': 256,
            'conv3': 512,
            'conv4': 1024,
            'bridge': 1024,
            'up1': 512,
            'up2': 256,
            'up3': 128,
            'up4': 64,
        }
        self.downscale_factors = {
            'input': 1,
            'conv1': 2,
            'conv2': 4,
            'conv3': 8,
            'conv4': 16,
            'bridge': 16,
            'up1': 8,
            'up2': 4,
            'up3': 2,
            'up4': 1,
        }
        self.bridge = Bridge(self.d['conv4'], self.d['bridge'])
        self.up_blocks = nn.ModuleList([
            UpBlockForUNetWithResNet50(in_channels=self.d['up1'] + self.d['conv3'], out_channels=self.d['up1'],
                                       up_conv_in_channels=self.d['bridge'], up_conv_out_channels=self.d['up1']),
            UpBlockForUNetWithResNet50(in_channels=self.d['up2'] + self.d['conv2'], out_channels=self.d['up2'],
                                       up_conv_in_channels=self.d['up1'], up_conv_out_channels=self.d['up2']),
            UpBlockForUNetWithResNet50(in_channels=self.d['up3'] + self.d['conv1'], out_channels=self.d['up3'],
                                       up_conv_in_channels=self.d['up2'], up_conv_out_channels=self.d['up3']),
            UpBlockForUNetWithResNet50(in_channels=self.d['up4'] + self.d['input'], out_channels=self.d['up4'],
                                       up_conv_in_channels=self.d['up3'], up_conv_out_channels=self.d['up4']) # concatenated with input
        ])
        self.out_size = out_size
        self.dropout = nn.Dropout(p=0.2)
        self.seg_classifier = nn.Conv1d(self.d['up4'], out_size , kernel_size=1, bias=True)

    def _get_basemodel(self, model_name, pretrained=False, layers=['blocks.9']):
        # try:
        ''' visual backbone'''
        net_dict = {"resnet18": models.resnet18(pretrained=pretrained),
                        "resnet50": models.resnet50(pretrained=pretrained),
                        "ViT-B/16": models.vit_b_16(torchvision.models.ViT_B_16_Weights.IMAGENET1K_SWAG_LINEAR_V1),
                        "ViT-L/16": models.vit_l_16(torchvision.models.ViT_L_16_Weights.IMAGENET1K_SWAG_LINEAR_V1)}
        if "resnet" in model_name:
            model = net_dict[model_name]
            num_ftrs = int(model.fc.in_features)
            backbone = nn.Sequential(*list(model.children())[:-3])
        elif 'timm-' in model_name:
            model_name = model_name.replace('timm-', '')
            model = timm.create_model(model_name, pretrained=True)
            backbone = create_feature_extractor(model, return_nodes={layers[0]: 'layer'})
            num_ftrs = backbone.patch_embed.proj.out_channels
        elif "ViT" in model_name:
            model = net_dict[model_name]
            backbone = create_feature_extractor(model, return_nodes={'encoder.ln': 'layer'}) 
            num_ftrs = model.hidden_dim
        return backbone, num_ftrs

    def forward(self, img):
        x = img
        down_embdding = [x]
        for i in range(len(self.backbone)):
            x = self.backbone[i](x)
            if i == 2 or i == 4 or i == 5:
                down_embdding.append(x)
        
        o = self.bridge(x)
        
        for i in range(len(self.up_blocks)):
            o = self.up_blocks[i](o,down_embdding[len(down_embdding) - i - 1])
        o = self.dropout(o)
        batch_size = o.shape[0]
        h = o.shape[-2]
        w = o.shape[-1]
        class_number = o.shape[-3]
        o = o.reshape(batch_size,class_number,h*w)
        o = self.seg_classifier(o)
        o = o.reshape(batch_size,self.out_size, h ,w) 
        return o
    