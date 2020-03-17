import torch.nn as nn
import os
import json
import torch
import math
import torch.utils.data as tdata
import torch.optim as optim
from torch.utils.data._utils.collate import default_collate
import numpy as np
from tqdm import tqdm
import tensorboardX
import argparse


if __name__ == '__main__':
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from scripts.action_recognition import ACTION_REG_LOG_DIR, ACTION_REG_CHECKPOINT_DIR, ACTION_REG_CONFIG_DIR
from scripts import set_determinstic_mode
import data.breakfast as breakfast
from nets.action_reg import rnn


CHECKPOINT_DIR = os.path.join(ACTION_REG_CHECKPOINT_DIR, 'rnn')
LOG_DIR = os.path.join(ACTION_REG_LOG_DIR, 'rnn')
CONFIG_DIR = os.path.join(ACTION_REG_CONFIG_DIR, 'rnn')


# I3D_N_CHANNELS = 400
NUM_WORKERS = 2


class Trainer:
    def __init__(self, experiment, device):
        config_file = os.path.join(CONFIG_DIR, experiment + '.json')
        assert os.path.exists(config_file), 'config file {} does not exist'.format(config_file)
        self.experiment = experiment
        with open(config_file, 'r') as f:
            configs = json.load(f)
        self.device = int(device)
        self.i3d_length = configs['i3d-length']
        self.stride = configs['stride']

        self.lr = configs['lr']
        self.max_epochs = configs['max-epochs']
        self.train_batch_size = configs['train-batch-size']
        self.test_batch_size = configs['test-batch-size']
        self.n_epochs = 0
        self.n_test_segments = configs['n-test-segments']

        self.log_dir = os.path.join(LOG_DIR, experiment)
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        self.tboard_writer = tensorboardX.SummaryWriter(log_dir=self.log_dir)

        self.checkpoint_dir = os.path.join(CHECKPOINT_DIR, experiment)
        if not os.path.exists(self.checkpoint_dir):
            os.makedirs(self.checkpoint_dir)

        self.hidden_size = configs['hidden-size']
        model_id = configs['model-id']
        if model_id == 'baseline':
            self.model = rnn.Baseline(n_inputs=self.i3d_length, n_classes=breakfast.N_CLASSES,
                                      hidden_size=self.hidden_size, aggregate=configs['aggregate'])
        else:
            raise ValueError('no such model')
        self.model = self.model.cuda(self.device)
        self.loss_fn = nn.CrossEntropyLoss().cuda(self.device)
        if configs['optim'] == 'adam':
            self.optimizer = optim.Adam(self.model.parameters(), lr=self.lr)
        elif configs['optim'] == 'sgd':
            self.optimizer = optim.SGD(self.model.parameters(), lr=self.lr, momentum=configs['momentum'],
                                       nesterov=configs['nesterov'])
        else:
            raise ValueError('no such optimizer')

        if configs['scheduler'] == 'step':
            self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, step_size=configs['lr-step'],
                                                       gamma=configs['lr-decay'])
        elif configs['scheduler'] == 'plateau':
            self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min',
                                                                  patience=configs['lr-step'])
        else:
            raise ValueError('no such scheduler')
        self._load_checkpoint()

    def train(self, train_data, test_data):
        train_segments, train_labels, train_logits = train_data
        test_segments, test_labels, test_logits = test_data

        train_dataset = TrainDataset(train_segments, train_labels, train_logits, i3d_length=self.i3d_length,
                                     stride=self.stride)
        test_dataset = TestDataset(test_segments, test_labels, test_logits, stride=self.stride,
                                   i3d_length=self.i3d_length)
        train_val_dataset = TestDataset(train_segments, train_labels, train_logits, i3d_length=self.i3d_length,
                                        stride=self.stride)

        start_epoch = self.n_epochs
        for epoch in range(start_epoch, self.max_epochs):
            self.n_epochs += 1
            self.train_step(train_dataset)
            self._save_checkpoint('model-{}'.format(self.n_epochs))
            self._save_checkpoint()  # update the latest model
            train_acc = self.test_step(train_val_dataset)
            test_acc = self.test_step(test_dataset)
            print('INFO: at epoch {}, the train accuracy is {} and the test accuracy is {}'.format(self.n_epochs,
                                                                                                   train_acc, test_acc))
            log_dict = {
                'train': train_acc,
                'test': test_acc
            }
            self.tboard_writer.add_scalars('accuracy', log_dict, self.n_epochs)

            if isinstance(self.scheduler, optim.lr_scheduler.StepLR):
                self.scheduler.step(epoch)

    def train_step(self, train_dataset):
        print('INFO: training at epoch {}'.format(self.n_epochs))
        dataloader = tdata.DataLoader(train_dataset, shuffle=True, batch_size=self.train_batch_size, drop_last=True,
                                      collate_fn=train_dataset.collate_fn, pin_memory=True, num_workers=NUM_WORKERS)
        self.model.train()
        losses = []
        for feats, segment_lens, logits in tqdm(dataloader):
            feats = feats.cuda(self.device)
            segment_lens = segment_lens.cuda(self.device)
            logits = logits.cuda(self.device)

            self.optimizer.zero_grad()
            feats = self.model(feats, segment_lens)
            loss = self.loss_fn(feats, logits)
            loss.backward()
            self.optimizer.step()
            losses.append(loss.item())
        avg_loss = np.mean(losses)
        print('INFO: at epoch {0} loss = {1}'.format(self.n_epochs, avg_loss))
        self.tboard_writer.add_scalar('loss', avg_loss, self.n_epochs)

        if isinstance(self.scheduler, optim.lr_scheduler.ReduceLROnPlateau):
            self.scheduler.step(avg_loss)

    def test_step(self, test_dataset):
        dataloader = tdata.DataLoader(test_dataset, shuffle=False, batch_size=self.test_batch_size,
                                      collate_fn=test_dataset.collate_fn, pin_memory=True, num_workers=NUM_WORKERS)
        self.model.eval()
        n_correct = 0
        n_predictions = 0
        with torch.no_grad():
            for feats, segment_lens, logits in tqdm(dataloader):
                feats = feats.cuda(self.device)
                segment_lens = segment_lens.cuda(self.device)
                logits = logits.cuda(self.device)

                feats = self.model(feats, segment_lens)
                predictions = torch.argmax(feats, dim=1)

                for i, prediction in enumerate(predictions):
                    if prediction == logits[i]:
                        n_correct += 1
                n_predictions += predictions.shape[0]
            accuracy = n_correct / n_predictions
        return accuracy

    def _save_checkpoint(self, checkpoint_name='model'):
        if not os.path.exists(self.checkpoint_dir):
            os.makedirs(self.checkpoint_dir)
        checkpoint_file = os.path.join(self.checkpoint_dir, checkpoint_name + '.pth')
        save_dict = {
            'model': self.model.state_dict(),
            'optim': self.optimizer.state_dict(),
            'scheduler': self.scheduler.state_dict(),
            'n-epochs': self.n_epochs
        }
        torch.save(save_dict, checkpoint_file)

    def _load_checkpoint(self, checkpoint_name='model'):
        checkpoint_file = os.path.join(self.checkpoint_dir, checkpoint_name + '.pth')
        if os.path.exists(checkpoint_file):
            print('INFO: loading checkpoint {}'.format(checkpoint_file))
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['model'])
            self.optimizer.load_state_dict(checkpoint['optim'])
            self.scheduler.load_state_dict(checkpoint['scheduler'])
            self.n_epochs = checkpoint['n-epochs']
        else:
            print('INFO: checkpoint does not exist, continuing...')

    def predict(self, prediction_segments):
        dataset = PredictionDataset(prediction_segments, i3d_length=self.i3d_length, stride=self.stride)
        dataloader = tdata.DataLoader(dataset, shuffle=False, batch_size=self.test_batch_size, num_workers=NUM_WORKERS,
                                      pin_memory=True)
        self.model.eval()
        all_predictions = []
        with torch.no_grad():
            for feats in tqdm(dataloader):
                feats = feats.cuda(self.device)
                feats = feats.view(-1, self.i3d_length)
                feats = self.model(feats)
                feats = feats.view(-1, self.n_test_segments, breakfast.N_CLASSES)
                feats = torch.sum(feats, dim=1)
                predictions = torch.argmax(feats, dim=1)
                predictions = predictions.detach().cpu().tolist()
                all_predictions.extend(predictions)
        return all_predictions


class TrainDataset(tdata.Dataset):
    def __init__(self, segments, segment_labels, segment_logits, i3d_length, stride):
        super(TrainDataset, self).__init__()
        self.segments = segments
        self.segment_labels = segment_labels
        self.segment_logits = segment_logits
        self.i3d_length = int(i3d_length)
        self.stride = int(stride)

    def __getitem__(self, idx):
        segment_dict = self.segments[idx]
        logit = self.segment_logits[idx]
        video_name = segment_dict['video-name']
        start, end = segment_dict['start'], segment_dict['end']
        assert start < end, '{0} has errors, logit {1}'.format(video_name, logit)

        i3d_feat = breakfast.read_i3d_data(video_name, window=[start, end], i3d_length=self.i3d_length)
        assert len(i3d_feat) > 0, '{0} has length {1}, logit {2}'.format(video_name, len(i3d_feat), logit)
        i3d_feat = i3d_feat[::self.stride]
        i3d_feat = torch.from_numpy(i3d_feat)
        n_feats = i3d_feat.shape[0]
        return i3d_feat, n_feats, logit

    def __len__(self):
        return len(self.segments)

    @staticmethod
    def collate_fn(batch):
        feats, n_feats, logits = zip(*batch)
        # the feats have all different lengths
        feats = torch.cat(feats, dim=0)
        n_feats = default_collate(n_feats)
        logits = default_collate(logits)
        return feats, n_feats, logits


class TestDataset(TrainDataset):  # placeholder class
    pass


class PredictionDataset(TestDataset):
    def __init__(self, segments, i3d_length, stride):
        super(PredictionDataset, self).__init__(segments=segments, segment_labels=None, segment_logits=None,
                                                i3d_length=i3d_length, stride=stride)

    def __getitem__(self, idx):
        segment = self.segments[idx]
        video_name = segment['video-name']
        start, end = segment['start'], segment['end']

        i3d_feat = breakfast.read_i3d_data(video_name, window=[start, end], i3d_length=self.i3d_length)
        i3d_feat = i3d_feat[::self.stride]
        i3d_feat = torch.from_numpy(i3d_feat)
        segment_len = i3d_feat.shape[0]
        return i3d_feat, segment_len

    @staticmethod
    def collate_fn(batch):
        i3d_feats, segment_lens = zip(*batch)
        i3d_feats = torch.cat(batch, dim=0)
        segment_lens = default_collate(segment_lens)
        return i3d_feats, segment_lens


def _parse_args():
    argparser = argparse.ArgumentParser()
    argparser.add_argument('-c', '--config', required=True, type=str, help='config filename e.g -c base')
    argparser.add_argument('-d', '--device', default=0, choices=np.arange(torch.cuda.device_count()),
                           type=int, help='device to run on')
    return argparser.parse_args()


def _parse_split_data(split, feat_len):
    segments, labels, logits = breakfast.get_data(split)
    valid_segments = []
    valid_labels = []
    valid_logits = []
    for i, segment in enumerate(tqdm(segments)):
        start, end = segment['start'], segment['end']
        i3d_feats = breakfast.read_i3d_data(segment['video-name'], window=[start, end], i3d_length=feat_len)
        if len(i3d_feats) > 0 and 48 > logits[i] > 0:  # remove walk in and walk out.
            segment['end'] = segment['start'] + len(i3d_feats)
            valid_segments.append(segment)
            valid_labels.append(labels[i])
            valid_logits.append(logits[i])
    return [valid_segments, valid_labels, valid_logits]


def main():
    set_determinstic_mode()
    args = _parse_args()
    trainer = Trainer(args.config, args.device)
    train_data = _parse_split_data('train', trainer.i3d_length)
    test_data = _parse_split_data('test', trainer.i3d_length)
    trainer.train(train_data, test_data)


if __name__ == '__main__':
    main()
