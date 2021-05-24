from __future__ import absolute_import
from __future__ import division

from torch import nn
import math
from .eca_module import eca_layer

import torch.utils.model_zoo as model_zoo
from copy import deepcopy

from torchreid.components import branches
from torchreid.components.shallow_cam import ShallowCAM

import logging

logger = logging.getLogger(__name__)


model_urls = {
    'resnet18': 'https://download.pytorch.org/models/resnet18-5c106cde.pth',
    'resnet34': 'https://download.pytorch.org/models/resnet34-333f7ec4.pth',
    'resnet50': 'https://www.dropbox.com/s/9ho5e1jq49l7t7p/eca_resnet50.pth.tar?dl=1',
    #'resnet50': 'https://www.dropbox.com/s/96qjbmxu6jfsdsg/eca_resnet50_k3557.pth.tar?dl=1',
    #'resnet50': 'home/seecs/Thesis/ABD-Net-master/eca_resnet50_k3557.pth.tar',
    #'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    'resnet101': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    'resnet152': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
}


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class ECABasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, k_size=3):
        super(ECABasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes, 1)
        self.bn2 = nn.BatchNorm2d(planes)
        self.eca = eca_layer(planes, k_size)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.eca(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, k_size=3):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.eca = eca_layer(planes * 4, k_size)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)
        out = self.eca(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class ResNet(nn.Module):
    """
    Residual network

    Reference:
    He et al. Deep Residual Learning for Image Recognition. CVPR 2016.
    """

    def __init__(self, block, layers, num_classes, k_size=[3, 3, 3, 3]):
        self.inplanes = 64
        super(ResNet, self).__init__()

        # backbone network
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0], int(k_size[0]))
        self.layer2 = self._make_layer(block, 128, layers[1], int(k_size[1]), stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], int(k_size[2]), stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], int(k_size[3]), stride=2)
        self.avgpool = nn.AvgPool2d(7, stride=1)
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _make_layer(self, block, planes, blocks, k_size, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, k_size))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes, k_size=k_size))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)

        return x

def eca_resnet18(k_size=[3, 3, 3, 3], num_classes=1_000, pretrained=False):
    """Constructs a ResNet-18 model.
    Args:
        k_size: Adaptive selection of kernel size
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        num_classes:The classes of classification
    """
    model = ResNet(ECABasicBlock, [2, 2, 2, 2], num_classes=num_classes, k_size=k_size)
    model.avgpool = nn.AdaptiveAvgPool2d(1)
    return model


def eca_resnet34(k_size=[3, 3, 3, 3], num_classes=1_000, pretrained=False):
    """Constructs a ResNet-34 model.
    Args:
        k_size: Adaptive selection of kernel size
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        num_classes:The classes of classification
    """
    model = ResNet(ECABasicBlock, [3, 4, 6, 3], num_classes=num_classes, k_size=k_size)
    model.avgpool = nn.AdaptiveAvgPool2d(1)
    return model


def eca_resnet50(k_size=[3, 3, 3, 3], num_classes=1000, pretrained=False):
    """Constructs a ResNet-50 model.
    Args:
        k_size: Adaptive selection of kernel size
        num_classes:The classes of classification
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    print("Constructing eca_resnet50......")
    model = ResNet(Bottleneck, [3, 4, 6, 3], num_classes=num_classes, k_size=k_size)
    model.avgpool = nn.AdaptiveAvgPool2d(1)
    return model


def eca_resnet101(k_size=[3, 3, 3, 3], num_classes=1_000, pretrained=False):
    """Constructs a ResNet-101 model.
    Args:
        k_size: Adaptive selection of kernel size
        num_classes:The classes of classification
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(Bottleneck, [3, 4, 23, 3], num_classes=num_classes, k_size=k_size)
    model.avgpool = nn.AdaptiveAvgPool2d(1)
    return model


def eca_resnet152(k_size=[3, 3, 3, 3], num_classes=1_000, pretrained=False):
    """Constructs a ResNet-152 model.
    Args:
        k_size: Adaptive selection of kernel size
        num_classes:The classes of classification
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(ECABottleneck, [3, 8, 36, 3], num_classes=num_classes, k_size=k_size)
    model.avgpool = nn.AdaptiveAvgPool2d(1)
    return model

def init_pretrained_weights(model, model_url):
    """Initializes model with pretrained weights.

    Layers that don't match with pretrained layers in name or size are kept unchanged.
    """
    
    #pretrain_dict = model.load_state_dict(nn.load('home/seecs/Thesis/ABD-Net-master/eca_resnet50_k3557.pth.tar'))
    #pretrain_dict = nn.load('/home/seecs/Thesis/ABD-Net-master/eca_resnet50_k3557.pth.tar')
    
    
    #model = TheModelClass(*args, **kwargs)
    #model_dict = model.load_state_dict(torch.load(PATH))

    pretrain_dict = model_zoo.load_url(model_url)
    model_dict = model.state_dict()
    pretrain_dict = {k: v for k, v in pretrain_dict.items() if k in model_dict and model_dict[k].size() == v.size()}
    model_dict.update(pretrain_dict)
    model.load_state_dict(model_dict)
    print('Initialized model with pretrained weights from {}'.format(model_url))


class ResNetCommonBranch(nn.Module):

    def __init__(self, owner, backbone, args):

        super().__init__()

        self.backbone1 = nn.Sequential(
            backbone.conv1,
            backbone.bn1,
            backbone.relu,
            backbone.maxpool,
            backbone.layer1
        )
        self.shallow_cam = ShallowCAM(args, 256)
        self.backbone2 = nn.Sequential(
            backbone.layer2,
            backbone.layer3,
        )

    def backbone_modules(self):

        return [self.backbone1, self.backbone2]

    def forward(self, x):

        x = self.backbone1(x)
        intermediate = x = self.shallow_cam(x)
        x = self.backbone2(x)

        return x, intermediate

class ResNetDeepBranch(nn.Module):

    def __init__(self, owner, backbone, args):

        super().__init__()

        self.backbone = deepcopy(backbone.layer4)

        self.out_dim = 2048

    def backbone_modules(self):

        return [self.backbone]

    def forward(self, x):
        return self.backbone(x)

class ResNetMGNLikeCommonBranch(nn.Module):

    def __init__(self, owner, backbone, args):

        super().__init__()

        self.backbone1 = nn.Sequential(
            backbone.conv1,
            backbone.bn1,
            backbone.relu,
            backbone.maxpool,
            backbone.layer1
        )
        self.shallow_cam = ShallowCAM(args, 256)
        self.backbone2 = nn.Sequential(
            backbone.layer2,
            backbone.layer3[0],
        )

    def backbone_modules(self):

        return [self.backbone1, self.backbone2]

    def forward(self, x):

        x = self.backbone1(x)
        intermediate = x = self.shallow_cam(x)
        x = self.backbone2(x)

        return x, intermediate

class ResNetMGNLikeDeepBranch(nn.Module):

    def __init__(self, owner, backbone, args):

        super().__init__()

        self.backbone = nn.Sequential(
            *deepcopy(backbone.layer3[1:]),
            deepcopy(backbone.layer4)
        )
        self.out_dim = 2048

    def backbone_modules(self):

        return [self.backbone]

    def forward(self, x):
        return self.backbone(x)


class MultiBranchResNet(branches.MultiBranchNetwork):

    def _get_common_branch(self, backbone, args):

        return ResNetCommonBranch(self, backbone, args)

    def _get_middle_subbranch_for(self, backbone, args, last_branch_class):

        return ResNetDeepBranch(self, backbone, args)

class MultiBranchMGNLikeResNet(branches.MultiBranchNetwork):

    def _get_common_branch(self, backbone, args):

        return ResNetMGNLikeCommonBranch(self, backbone, args)

    def _get_middle_subbranch_for(self, backbone, args, last_branch_class):

        return ResNetMGNLikeDeepBranch(self, backbone, args)


def resnet50_backbone():
    num_classes=1000
    #k_size=[3, 3, 3, 3]
    network = ResNet(block=Bottleneck, layers=[3, 4, 6, 3], num_classes=num_classes, k_size=k_size)
    #network = ResNet(block=Bottleneck, layers=[3, 4, 6, 3], num_classes=num_classes)
    network.avgpool = nn.AdaptiveAvgPool2d(1)
    init_pretrained_weights(network, model_urls['resnet50'])

    return network


def resnet50(num_classes, args, pretrained=False, k_size=[3, 3, 3, 3], **kw):
    
    print("Constructing ECA_Resnet50 ......")
    backbone = resnet50_backbone()
    return MultiBranchResNet(backbone, args, num_classes)

def resnet50_mgn_like(num_classes, args, **kw):

    backbone = resnet50_backbone()
    return MultiBranchMGNLikeResNet(backbone, args, num_classes)
