
import os

import cv2
import numpy as np
from PIL import Image

import torch
from torch.nn import functional as F

from .base_dataset import BaseDataset


class CICV(BaseDataset):
    def __init__(self,
                 root,
                 list_path,
                 is_test=False,
                 num_samples=None,
                 num_classes=10,
                 multi_scale=True,
                 flip=False,
                 ignore_label=-1,
                 base_size=1536,
                 crop_size=(512, 1024),
                 downsample_rate=1,
                 scale_factor=16,
                 mean=[0.485, 0.456, 0.406],
                 std=[0.229, 0.224, 0.225]):
        """
        Args: 
        * root: 数据集的根目录
        * list_path: 图像 txt 文件, root 相对路径
        * [new] is_test: 是否测试用(不加载标签等...)
        * num_samples: 选择的样本数量, None 标识全部使用
        * num_classes: 类别数量
        * multi_scale: T, multi-scale training
        * flip: T, 是否随机翻转
        * ignore_label: 忽略的类别
        ...
        """

        super(CICV, self).__init__(
            ignore_label, base_size, crop_size, downsample_rate, scale_factor, mean, std, )

        self.root = root
        self.list_path = list_path
        self.is_test = is_test
        self.num_classes = num_classes
        self.multi_scale = multi_scale
        self.flip = flip
        if self.flip is True:
            print("Warning: CIVI dataset 有的类别不是适合做翻转！！！\n" * 3)
        # 获取图像列表
        self.img_list = [line.strip().split()[0] for line in open(os.path.join(root, list_path))]

        self.files = self.read_files()
        if num_samples:
            self.files = self.files[:num_samples]

        """
        """
        # 忽略数量较少的类别（训练集小于 100）
        self.label_mapping = {-1: ignore_label,
                              }
        self.class_weights = None

    def read_files(self):
        files = []
        if self.is_test:
            for img_path in self.img_list:
                img_path = os.path.join(self.root, img_path)
                name = os.path.splitext(os.path.basename(img_path))[0]
                files.append({
                    "img": img_path,
                    "name": name,
                })
        else:
            for img_path in self.img_list:
                img_path = os.path.join(self.root, img_path)
                label_path = img_path.replace("/images/", "/labels-mask/").rsplit(".", 1)[0] + ".png"
                name = os.path.splitext(os.path.basename(label_path))[0]
                files.append({
                    "img": img_path,
                    "label": label_path,
                    "name": name,
                    "weight": 1
                })
        return files

    def convert_label(self, label, inverse=False):
        temp = label.copy()
        if inverse:
            for v, k in self.label_mapping.items():
                label[temp == k] = v
        else:
            for k, v in self.label_mapping.items():
                label[temp == k] = v
        return label

    def __getitem__(self, index):
        item = self.files[index]
        name = item["name"]
        # image = cv2.imread(os.path.join(self.root,'cityscapes',item["img"]),
        #                    cv2.IMREAD_COLOR)
        image = cv2.imread(item["img"], cv2.IMREAD_COLOR)
        size = image.shape

        if 'test' in self.list_path:
            image = self.input_transform(image)
            image = image.transpose((2, 0, 1))

            return image.copy(), np.array(size), name

        label = cv2.imread(item["label"], cv2.IMREAD_GRAYSCALE)
        label = self.convert_label(label)

        image, label = self.gen_sample(image, label,
                                       self.multi_scale, self.flip)

        return image.copy(), label.copy(), np.array(size), name

    def multi_scale_inference(self, config, model, image, scales=[1], flip=False):
        batch, _, ori_height, ori_width = image.size()
        assert batch == 1, "only supporting batchsize 1."
        image = image.numpy()[0].transpose((1, 2, 0)).copy()
        stride_h = np.int(self.crop_size[0] * 1.0)
        stride_w = np.int(self.crop_size[1] * 1.0)
        final_pred = torch.zeros([1, self.num_classes,
                                  ori_height, ori_width]).cuda()
        for scale in scales:
            new_img = self.multi_scale_aug(image=image,
                                           rand_scale=scale,
                                           rand_crop=False)
            height, width = new_img.shape[:-1]

            if scale <= 1.0:
                new_img = new_img.transpose((2, 0, 1))
                new_img = np.expand_dims(new_img, axis=0)
                new_img = torch.from_numpy(new_img)
                preds = self.inference(config, model, new_img, flip)
                preds = preds[:, :, 0:height, 0:width]
            else:
                new_h, new_w = new_img.shape[:-1]
                rows = np.int(np.ceil(1.0 * (new_h -
                                             self.crop_size[0]) / stride_h)) + 1
                cols = np.int(np.ceil(1.0 * (new_w -
                                             self.crop_size[1]) / stride_w)) + 1
                preds = torch.zeros([1, self.num_classes,
                                     new_h, new_w]).cuda()
                count = torch.zeros([1, 1, new_h, new_w]).cuda()

                for r in range(rows):
                    for c in range(cols):
                        h0 = r * stride_h
                        w0 = c * stride_w
                        h1 = min(h0 + self.crop_size[0], new_h)
                        w1 = min(w0 + self.crop_size[1], new_w)
                        h0 = max(int(h1 - self.crop_size[0]), 0)
                        w0 = max(int(w1 - self.crop_size[1]), 0)
                        crop_img = new_img[h0:h1, w0:w1, :]
                        crop_img = crop_img.transpose((2, 0, 1))
                        crop_img = np.expand_dims(crop_img, axis=0)
                        crop_img = torch.from_numpy(crop_img)
                        pred = self.inference(config, model, crop_img, flip)
                        preds[:, :, h0:h1, w0:w1] += pred[:, :, 0:h1 - h0, 0:w1 - w0]
                        count[:, :, h0:h1, w0:w1] += 1
                preds = preds / count
                preds = preds[:, :, :height, :width]

            preds = F.interpolate(
                preds, (ori_height, ori_width),
                mode='bilinear', align_corners=config.MODEL.ALIGN_CORNERS
            )
            final_pred += preds
        return final_pred

    def get_palette(self, n):
        palette = [0] * (n * 3)
        for j in range(0, n):
            lab = j
            palette[j * 3 + 0] = 0
            palette[j * 3 + 1] = 0
            palette[j * 3 + 2] = 0
            i = 0
            while lab:
                palette[j * 3 + 0] |= (((lab >> 0) & 1) << (7 - i))
                palette[j * 3 + 1] |= (((lab >> 1) & 1) << (7 - i))
                palette[j * 3 + 2] |= (((lab >> 2) & 1) << (7 - i))
                i += 1
                lab >>= 3
        return palette

    def save_pred(self, preds, sv_path, name):
        palette = self.get_palette(256)
        preds = np.asarray(np.argmax(preds.cpu(), axis=1), dtype=np.uint8)
        for i in range(preds.shape[0]):
            pred = self.convert_label(preds[i], inverse=True)
            save_img = Image.fromarray(pred)
            save_img.putpalette(palette)
            save_img.save(os.path.join(sv_path, name[i] + '.png'))
