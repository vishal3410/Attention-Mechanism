import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import argparse
import torch.utils.data.sampler as sampler

from create_dataset import *
from torch.autograd import Variable

parser = argparse.ArgumentParser(description='Multi-task: Cross')
parser.add_argument('--weight', default='equal', type=str, help='multi-task weighting: equal, uncert, dwa')
parser.add_argument('--dataroot', default='nyuv2', type=str, help='dataset root')
parser.add_argument('--temp', default=2.0, type=float, help='temperature for DWA (must be positive)')
opt = parser.parse_args()


class SegNet(nn.Module):
    def __init__(self):
        super(SegNet, self).__init__()
        # initialise network parameters
        filter = [64, 128, 256, 512, 512]
        self.class_nb = 13

        # define encoder decoder layers
        self.encoder_block_t = nn.ModuleList([nn.ModuleList([self.conv_layer([3, filter[0], filter[0]], bottle_neck=True)])])
        self.decoder_block_t = nn.ModuleList([nn.ModuleList([self.conv_layer([filter[0], filter[0], filter[0]], bottle_neck=True)])])

        for j in range(3):
            if j < 2:
                self.encoder_block_t.append(nn.ModuleList([self.conv_layer([3, filter[0], filter[0]], bottle_neck=True)]))
                self.decoder_block_t.append(nn.ModuleList([self.conv_layer([filter[0], filter[0], filter[0]], bottle_neck=True)]))
            for i in range(4):
                if i == 0:
                    self.encoder_block_t[j].append(self.conv_layer([filter[i], filter[i + 1], filter[i + 1]], bottle_neck=True))
                    self.decoder_block_t[j].append(self.conv_layer([filter[i + 1], filter[i], filter[i]], bottle_neck=True))
                else:
                    self.encoder_block_t[j].append(self.conv_layer([filter[i], filter[i + 1], filter[i + 1]], bottle_neck=False))
                    self.decoder_block_t[j].append(self.conv_layer([filter[i + 1], filter[i], filter[i]], bottle_neck=False))

        # define cross-stitch units
        self.cs_unit_encoder = nn.Parameter(data=torch.ones(4, 3))
        self.cs_unit_decoder = nn.Parameter(data=torch.ones(5, 3))

        # define task specific layers
        self.pred_task1 = self.conv_layer([filter[0], self.class_nb], bottle_neck=True, pred_layer=True)
        self.pred_task2 = self.conv_layer([filter[0], 1], bottle_neck=True, pred_layer=True)
        self.pred_task3 = self.conv_layer([filter[0], 3], bottle_neck=True, pred_layer=True)

        # define pooling and unpooling functions
        self.down_sampling = nn.MaxPool2d(kernel_size=2, stride=2, return_indices=True)
        self.up_sampling = nn.MaxUnpool2d(kernel_size=2, stride=2)

        self.logsigma = nn.Parameter(torch.FloatTensor([-0.5, -0.5, -0.5]))

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.xavier_uniform_(m.weight)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Parameter):
                nn.init.constant(m.weight, 1)

    def conv_layer(self, channel, bottle_neck, pred_layer=False):
        if bottle_neck:
            if not pred_layer:
                conv_block = nn.Sequential(
                    nn.Conv2d(in_channels=channel[0], out_channels=channel[1], kernel_size=3, padding=1),
                    nn.BatchNorm2d(channel[1]),
                    nn.ReLU(inplace=True),
                    nn.Conv2d(in_channels=channel[1], out_channels=channel[2], kernel_size=3, padding=1),
                    nn.BatchNorm2d(channel[2]),
                    nn.ReLU(inplace=True),
                )
            else:
                conv_block = nn.Sequential(
                    nn.Conv2d(in_channels=channel[0], out_channels=channel[0], kernel_size=3, padding=1),
                    nn.Conv2d(in_channels=channel[0], out_channels=channel[1], kernel_size=1, padding=0),
                )

        else:
            conv_block = nn.Sequential(
                nn.Conv2d(in_channels=channel[0], out_channels=channel[1], kernel_size=3, padding=1),
                nn.BatchNorm2d(channel[1]),
                nn.ReLU(inplace=True),
                nn.Conv2d(in_channels=channel[1], out_channels=channel[1], kernel_size=3, padding=1),
                nn.BatchNorm2d(channel[1]),
                nn.ReLU(inplace=True),
                nn.Conv2d(in_channels=channel[1], out_channels=channel[2], kernel_size=3, padding=1),
                nn.BatchNorm2d(channel[2]),
                nn.ReLU(inplace=True),
            )
        return conv_block

    def forward(self, x):
        encoder_conv_t, decoder_conv_t, encoder_samp_t, decoder_samp_t, indices_t = ([0] * 3 for _ in range(5))
        for i in range(3):
            encoder_conv_t[i], decoder_conv_t[i], encoder_samp_t[i], decoder_samp_t[i], indices_t[i] = ([0] * 5 for _ in range(5))

        # task branch 1
        for i in range(5):
            for j in range(3):
                if i == 0:
                    encoder_conv_t[j][i] = self.encoder_block_t[j][i](x)
                    encoder_samp_t[j][i], indices_t[j][i] = self.down_sampling(encoder_conv_t[j][i])
                else:
                    encoder_cross_stitch = self.cs_unit_encoder[i - 1][0] * encoder_samp_t[0][i - 1] + \
                                           self.cs_unit_encoder[i - 1][1] * encoder_samp_t[1][i - 1] + \
                                           self.cs_unit_encoder[i - 1][2] * encoder_samp_t[2][i - 1]
                    encoder_conv_t[j][i] = self.encoder_block_t[j][i](encoder_cross_stitch)
                    encoder_samp_t[j][i], indices_t[j][i] = self.down_sampling(encoder_conv_t[j][i])

        for i in range(5):
            for j in range(3):
                if i == 0:
                    decoder_cross_stitch = self.cs_unit_decoder[i][0] * encoder_samp_t[0][-1] + \
                                           self.cs_unit_decoder[i][1] * encoder_samp_t[1][-1] + \
                                           self.cs_unit_decoder[i][2] * encoder_samp_t[2][-1]
                    decoder_samp_t[j][i] = self.up_sampling(decoder_cross_stitch, indices_t[j][-i - 1])
                    decoder_conv_t[j][i] = self.decoder_block_t[j][-i - 1](decoder_samp_t[j][i])
                else:
                    decoder_cross_stitch = self.cs_unit_decoder[i][0] * decoder_conv_t[0][i - 1] + \
                                           self.cs_unit_decoder[i][1] * decoder_conv_t[1][i - 1] + \
                                           self.cs_unit_decoder[i][2] * decoder_conv_t[2][i - 1]
                    decoder_samp_t[j][i] = self.up_sampling(decoder_cross_stitch, indices_t[j][-i - 1])
                    decoder_conv_t[j][i] = self.decoder_block_t[j][-i - 1](decoder_samp_t[j][i])

        # define task prediction layers
        t1_pred = F.log_softmax(self.pred_task1(decoder_conv_t[0][-1]), dim=1)
        t2_pred = self.pred_task2(decoder_conv_t[1][-1])
        t3_pred = self.pred_task3(decoder_conv_t[2][-1])
        t3_pred = t3_pred / torch.norm(t3_pred, p=2, dim=1, keepdim=True)

        return [t1_pred, t2_pred, t3_pred], self.logsigma

    def model_fit(self, x_pred1, x_output1, x_pred2, x_output2, x_pred3, x_output3):
        # binary mark to mask out undefined pixel space
        binary_mask = (torch.sum(x_output2, dim=1) != 0).type(torch.FloatTensor).unsqueeze(1).to(device)

        # semantic loss: depth-wise cross entropy
        loss1 = F.nll_loss(x_pred1, x_output1, ignore_index=-1)

        # depth loss: l1 norm
        loss2 = torch.sum(torch.abs(x_pred2 - x_output2) * binary_mask) / torch.nonzero(binary_mask).size(0)

        # normal loss: dot product
        loss3 = 1 - torch.sum((x_pred3 * x_output3) * binary_mask) / torch.nonzero(binary_mask).size(0)

        return [loss1, loss2, loss3]

    def compute_miou(self, x_pred, x_output):
        _, x_pred_label = torch.max(x_pred, dim=1)
        x_output_label = x_output
        batch_size = x_pred.size(0)
        for i in range(batch_size):
            true_class = 0
            first_switch = True
            for j in range(self.class_nb):
                pred_mask = torch.eq(x_pred_label[i], Variable(j * torch.ones(x_pred_label[i].shape).type(torch.LongTensor).to(device)))
                true_mask = torch.eq(x_output_label[i], Variable(j * torch.ones(x_output_label[i].shape).type(torch.LongTensor).to(device)))
                mask_comb = pred_mask + true_mask
                union = torch.sum((mask_comb > 0).type(torch.FloatTensor))
                intsec = torch.sum((mask_comb > 1).type(torch.FloatTensor))
                if union == 0:
                    continue
                if first_switch:
                    class_prob = intsec / union
                    first_switch = False
                else:
                    class_prob = intsec / union + class_prob
                true_class += 1
            if i == 0:
                batch_avg = class_prob / true_class
            else:
                batch_avg = class_prob / true_class + batch_avg
        return batch_avg / batch_size

    def compute_iou(self, x_pred, x_output):
        _, x_pred_label = torch.max(x_pred, dim=1)
        x_output_label = x_output
        batch_size = x_pred.size(0)
        for i in range(batch_size):
            if i == 0:
                pixel_acc = torch.div(torch.sum(torch.eq(x_pred_label[i], x_output_label[i]).type(torch.FloatTensor)),
                                      torch.sum((x_output_label[i] >= 0).type(torch.FloatTensor)))
            else:
                pixel_acc = pixel_acc + torch.div(torch.sum(torch.eq(x_pred_label[i], x_output_label[i]).type(torch.FloatTensor)),
                                                  torch.sum((x_output_label[i] >= 0).type(torch.FloatTensor)))
        return pixel_acc / batch_size

    def depth_error(self, x_pred, x_output):
        binary_mask = (torch.sum(x_output, dim=1) != 0).unsqueeze(1).to(device)
        x_pred_true = x_pred.masked_select(binary_mask)
        x_output_true = x_output.masked_select(binary_mask)
        abs_err = torch.abs(x_pred_true - x_output_true)
        rel_err = torch.abs(x_pred_true - x_output_true) / x_output_true
        return torch.sum(abs_err) / torch.nonzero(binary_mask).size(0), torch.sum(rel_err) / torch.nonzero(binary_mask).size(0)

    def normal_error(self, x_pred, x_output):
        binary_mask = (torch.sum(x_output, dim=1) != 0)
        error = torch.acos(torch.clamp(torch.sum(x_pred * x_output, 1).masked_select(binary_mask), -1, 1)).detach().cpu().numpy()
        error = np.degrees(error)
        return np.mean(error), np.median(error), np.mean(error < 11.25), np.mean(error < 22.5), np.mean(error < 30)


# define model, optimiser and scheduler
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
SegNet_CROSS = SegNet().to(device)
optimizer = optim.Adam(SegNet_CROSS.parameters(), lr=1e-4)
scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=100, gamma=0.5)


# compute parameter space
def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


print('Parameter Space: ABS: {:.1f}, REL: {:.4f}\n'.format(count_parameters(SegNet_CROSS),
                                                           count_parameters(SegNet_CROSS)/24981069))
print('LOSS FORMAT: SEMANTIC_LOSS MEAN_IOU PIX_ACC | DEPTH_LOSS ABS_ERR REL_ERR | NORMAL_LOSS MEAN MED <11.25 <22.5 <30\n')

# define dataset path
dataset_path = opt.dataroot
nyuv2_train_set = NYUv2(root=dataset_path, train=True)
nyuv2_test_set = NYUv2(root=dataset_path, train=False)

batch_size = 2
nyuv2_train_loader = torch.utils.data.DataLoader(
    dataset=nyuv2_train_set,
    batch_size=batch_size,
    shuffle=True)

nyuv2_test_loader = torch.utils.data.DataLoader(
    dataset=nyuv2_test_set,
    batch_size=batch_size,
    shuffle=True)


# define parameters
total_epoch = 200
train_batch = len(nyuv2_train_loader)
test_batch = len(nyuv2_test_loader)
T = opt.temp
avg_cost = np.zeros([total_epoch, 24], dtype=np.float32)
lambda_weight = np.ones([3, total_epoch])
for epoch in range(total_epoch):
    index = epoch
    cost = np.zeros(24, dtype=np.float32)
    scheduler.step()

    # apply Dynamic Weight Average
    if opt.weight == 'dwa':
        if index == 0 or index == 1:
            lambda_weight[:, index] = 1.0
        else:
            w_1 = avg_cost[index - 1, 0] / avg_cost[index - 2, 0]
            w_2 = avg_cost[index - 1, 3] / avg_cost[index - 2, 3]
            w_3 = avg_cost[index - 1, 6] / avg_cost[index - 2, 6]
            lambda_weight[0, index] = 3 * np.exp(w_1 / T) / (np.exp(w_1 / T) + np.exp(w_2 / T) + np.exp(w_3 / T))
            lambda_weight[1, index] = 3 * np.exp(w_2 / T) / (np.exp(w_1 / T) + np.exp(w_2 / T) + np.exp(w_3 / T))
            lambda_weight[2, index] = 3 * np.exp(w_3 / T) / (np.exp(w_1 / T) + np.exp(w_2 / T) + np.exp(w_3 / T))

    # iteration for all batches
    nyuv2_train_dataset = iter(nyuv2_train_loader)
    for k in range(train_batch):
        train_data, train_label, train_depth, train_normal = nyuv2_train_dataset.next()
        train_data, train_label = train_data.to(device), train_label.type(torch.LongTensor).to(device)
        train_depth, train_normal = train_depth.to(device), train_normal.to(device)

        train_pred, logsigma = SegNet_CROSS(train_data)

        optimizer.zero_grad()
        train_loss = SegNet_CROSS.model_fit(train_pred[0], train_label, train_pred[1], train_depth, train_pred[2], train_normal)

        if opt.weight == 'equal' or opt.weight == 'dwa':
            loss = torch.mean(sum(lambda_weight[i, index] * train_loss[i] for i in range(3)))
        else:
            loss = sum(1 / (2 * torch.exp(logsigma[i])) * train_loss[i] + logsigma[i] / 2 for i in range(3))

        loss.backward()
        optimizer.step()

        cost[0] = train_loss[0].item()
        cost[1] = SegNet_CROSS.compute_miou(train_pred[0], train_label).item()
        cost[2] = SegNet_CROSS.compute_iou(train_pred[0], train_label).item()
        cost[3] = train_loss[1].item()
        cost[4], cost[5] = SegNet_CROSS.depth_error(train_pred[1], train_depth)
        cost[6] = train_loss[2].item()
        cost[7], cost[8], cost[9], cost[10], cost[11] = SegNet_CROSS.normal_error(train_pred[2], train_normal)
        avg_cost[index, :12] += cost[:12] / train_batch

    # evaluating test data
    with torch.no_grad():  # operations inside don't track history
        nyuv2_test_dataset = iter(nyuv2_test_loader)
        for k in range(test_batch):
            test_data, test_label, test_depth, test_normal = nyuv2_test_dataset.next()
            test_data, test_label = test_data.to(device),  test_label.type(torch.LongTensor).to(device)
            test_depth, test_normal = test_depth.to(device), test_normal.to(device)

            test_pred, _ = SegNet_CROSS(test_data)
            test_loss = SegNet_CROSS.model_fit(test_pred[0], test_label, test_pred[1], test_depth, test_pred[2], test_normal)

            cost[12] = test_loss[0].item()
            cost[13] = SegNet_CROSS.compute_miou(test_pred[0], test_label).item()
            cost[14] = SegNet_CROSS.compute_iou(test_pred[0], test_label).item()
            cost[15] = test_loss[1].item()
            cost[16], cost[17] = SegNet_CROSS.depth_error(test_pred[1], test_depth)
            cost[18] = test_loss[2].item()
            cost[19], cost[20], cost[21], cost[22], cost[23] = SegNet_CROSS.normal_error(test_pred[2], test_normal)

            avg_cost[index, 12:] += cost[12:] / test_batch


    print('Epoch: {:04d} | TRAIN: {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} '
          'TEST: {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} '
          .format(index, avg_cost[index, 0], avg_cost[index, 1], avg_cost[index, 2], avg_cost[index, 3],
                avg_cost[index, 4], avg_cost[index, 5], avg_cost[index, 6], avg_cost[index, 7], avg_cost[index, 8], avg_cost[index, 9],
                avg_cost[index, 10], avg_cost[index, 11], avg_cost[index, 12], avg_cost[index, 13],
                avg_cost[index, 14], avg_cost[index, 15], avg_cost[index, 16], avg_cost[index, 17], avg_cost[index, 18],
                avg_cost[index, 19], avg_cost[index, 20], avg_cost[index, 21], avg_cost[index, 22], avg_cost[index, 23]))

