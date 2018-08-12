import torch.nn as nn
import torch
import numpy as np


def parse_cfg(cfg):

    blocks = []
    with open(cfg) as f:
        lines = f.read().split('\n')
    lines = [l.strip() for l in lines]
    lines = [l for l in lines if len(l) > 0 and l[0] != '#']
    block = {}
    for line in lines:
        if line[0] == '[':
            if len(block) != 0:
                blocks.append(block)
                block = {}
            block['type'] = line[1:-1].strip()
        else:
            key, value = line.split('=')
            key = key.strip()
            value = value.strip()
            block[key] = value
    blocks.append(block)

    return blocks


class ShortcutLayer(nn.Module):
    def __init__(self, idx):
        super(ShortcutLayer, self).__init__()
        self.idx = idx

    def forward(self, outputs):
        return outputs[self.idx]

class RouteLayer(nn.Module):
    def __init__(self, indices):
        super(RouteLayer, self).__init__()
        self.indices = indices

    def forward(self, outputs):
        out = [outputs[i] for i in self.indices]
        out = torch.cat(out, dim=1)
        return out

class DetectionLayer(nn.Module):
    def __init__(self, anchors, num_classes, input_dim):
        super(DetectionLayer, self).__init__()
        self.anchors = torch.tensor(anchors, dtype=torch.float)
        self.num_classes = num_classes
        self.num_anchors = len(anchors)
        self.input_dim = input_dim

    def forward(self, x):
        batch_size = x.size(0)
        grid_size = x.size(2)
        stride = self.input_dim // grid_size

        detection = x.view(batch_size, self.num_anchors, self.num_classes + 5, grid_size, grid_size)
        # box centers
        detection[:, :, :2, :, :] = torch.sigmoid(detection[:, :, :2, :, :])
        #objectness score and class scores
        detection[:, :, 4:, :, :] = torch.sigmoid(detection[:, :, 4:, :, :])

        # add offset to box centers

        x_offset, y_offset = np.meshgrid(np.arange(grid_size), np.arange(grid_size), indexing='xy')
        x_offset = torch.from_numpy(x_offset).float()
        y_offset = torch.from_numpy(y_offset).float()

        x_offset = x_offset.expand_as(detection[:, :, 0, :, :])
        y_offset = y_offset.expand_as(detection[:, :, 1, :, :])
        detection[:, :, 0, :, :] += x_offset
        detection[:, :, 1, :, :] += y_offset
        detection[:, :, :2, :, :] *= stride
        # box width and height
        anchors = self.anchors.unsqueeze(-1).unsqueeze(-1).expand_as(detection[:, :, 2:4, :, :])
        detection[:, :, 2:4, :, :] = torch.exp(detection[:, :, 2:4, :, :]) * anchors

        detection = detection.transpose(1, 2).contiguous().view(batch_size, self.num_classes+5, -1).transpose(1, 2)
        return detection

def create_modules(blocks):
    net_info = blocks[0]
    module_list = nn.ModuleList()
    in_channel = 3
    out_channel = in_channel
    out_channels = []

    for i, block in enumerate(blocks[1:]):
        module = nn.Sequential()
        block_type = block['type']
        if block_type == 'convolutional':
            if 'batch_normalize' in block.keys():
                bn = True
                bias = False
            else:
                bn = False
                bias = True
            filters = int(block['filters'])
            kernel_size = int(block['size'])
            stride = int(block['stride'])
            pad = int(block['pad'])
            activation = block['activation']

            if pad:
                padding = (kernel_size-1) // 2
            else:
                padding = 0

            conv = nn.Conv2d(in_channels=in_channel, out_channels=filters, kernel_size=kernel_size, stride=stride, padding=padding, bias=bias)
            module.add_module('conv_%d' % (i), conv)

            if bn:
                module.add_module('batchnorm_%d' %(i), nn.BatchNorm2d(filters))
            if activation == 'leaky':
                module.add_module('leaky_%d' % i, nn.LeakyReLU(0.1, inplace=True))

            out_channel = filters

        elif block_type == 'shortcut':
            idx = int(block['from']) + i
            shortcut_layer = ShortcutLayer(idx)
            module.add_module('shortcut_%d' % i, shortcut_layer)

        elif block_type == 'upsample':
            stride = int(block['stride'])
            upsample_layer = nn.Upsample(scale_factor=stride, mode='bilinear')
            module.add_module('upsample_%d' % i, upsample_layer)

        elif block_type == 'route':
            layer_indices = block['layers'].split(',')
            first_idx = int(layer_indices[0])
            if first_idx < 0:
                first_idx = i + first_idx
            if len(layer_indices) > 1:
                second_idx = int(layer_indices[1])
                if second_idx < 0:
                    second_idx += i
                out_channel = out_channels[first_idx] + out_channels[second_idx]
                route_layer = RouteLayer([first_idx, second_idx])
            else:
                out_channel = out_channels[first_idx]
                route_layer = RouteLayer([first_idx])

            module.add_module('route_%d' % i, route_layer)


        elif block_type == 'yolo':
            masks = block['mask'].split(',')
            masks = [int(mask) for mask in masks]
            anchors = block['anchors'].split(',')
            anchors = [[int(anchors[2*i]), int(anchors[2*i+1])] for i in masks]
            num_classes = int(block['classes'])
            input_dim = int(net_info['width'])
            detection_layer = DetectionLayer(anchors, num_classes, input_dim)

            module.add_module('detection_%d' % i, detection_layer)

        out_channels.append(out_channel)
        in_channel = out_channel
        module_list.append(module)

    return (net_info, module_list)

class Darknet(nn.Module):
    def __init__(self, cfg):
        super(Darknet, self).__init__()
        self.blocks = parse_cfg(cfg)
        self.net_info, self.module_list = create_modules(self.blocks)

    def forward(self, x):
        blocks = self.blocks[1:]
        outputs = []
        detection = torch.tensor([], dtype=torch.float)
        for i, module in enumerate(self.module_list):
            block_type = blocks[i]['type']
            if block_type == 'convolutional' or block_type == 'upsample':
                x = module(x)
            elif block_type == 'shortcut':
                x += module(outputs)
            elif block_type == 'route':
                x = module(outputs)
            elif block_type == 'yolo':
                x = module(x)
                detection = torch.cat((x, detection), dim=1)

            outputs.append(x)

        return detection

    def load_weights(self, file):
        with open(file, 'rb') as f:
            header = np.fromfile(f, np.int32, count=5)
            weights = np.fromfile(f, np.float32)
        self.header = torch.from_numpy(header)
        ptr = 0

        for i in range(len(self.module_list)):
            module = self.module_list[i]
            block_type = self.blocks[i+1]['type']

            if block_type == 'convolutional':
                conv = module[0]
                if 'batch_normalize' in self.blocks[i+1].keys():
                    bn = module[1]
                    num_weights = bn.weight.numel()

                    bn_bias = torch.from_numpy(weights[ptr: ptr + num_weights]).view_as(bn.bias.data)
                    ptr += num_weights
                    bn_weight = torch.from_numpy(weights[ptr: ptr + num_weights]).view_as(bn.weight.data)
                    ptr += num_weights

                    bn_running_mean = torch.from_numpy(weights[ptr: ptr + num_weights]).view_as(bn.running_mean)
                    ptr += num_weights
                    bn_running_var = torch.from_numpy(weights[ptr: ptr + num_weights]).view_as(bn.running_var)
                    ptr += num_weights

                    bn.weight.data.copy_(bn_weight)
                    bn.bias.data.copy_(bn_bias)
                    bn.running_mean.copy_(bn_running_mean)
                    bn.running_var.copy_(bn_running_var)
                else:
                    num_bias = conv.bias.numel()
                    conv_bias = torch.from_numpy(weights[ptr: ptr + num_bias]).view_as(conv.bias.data)
                    ptr += num_bias
                    conv.bias.data.copy_(conv_bias)

                num_weights = conv.weight.numel()
                conv_weight = torch.from_numpy(weights[ptr: ptr + num_weights]).view_as(conv.weight.data)
                ptr += num_weights
                conv.weight.data.copy_(conv_weight)
