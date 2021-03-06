#!/usr/bin/env python

import os, sys, cv2, math, PIL, json
import numpy as np
import pickle, random
import os.path as osp
from time import time
from tqdm import tqdm
from copy import deepcopy
from glob import glob
import matplotlib.pyplot as plt
from collections import OrderedDict

from layout_config import get_config
from layout_utils import *

import torch, torchtext
from torch.utils.data import Dataset
from transformers import BertTokenizer
from datasets.layout_coco import layout_coco, layout_coco_bert
        

class layout_vg_bert(layout_coco):
    def __init__(self, config, split=None, transform=None):
        self.cfg = config
        self.tokenizer_path = 'bert-base' if self.cfg.bert_path=='' else os.path.join(self.cfg.bert_path, 'bert-base-uncased-vocab.txt')

        self.split = split
        if split is not None:
            self.name = 'layout_vg' + '_' + split
        else:
            self.name = 'layout_vg'

        self.max_len = 0
        self.min_len = 500
        self.transform = transform
        self.root_dir  = osp.join(config.data_dir, 'vg')
        self.cache_dir = osp.abspath(osp.join(config.data_dir, 'caches'))
        maybe_create(self.cache_dir)
        self.cache_file = osp.join(self.cache_dir, 'bert_layout_vg_' + self.split + '.pkl')
        
        self.tokenizer = BertTokenizer.from_pretrained(self.tokenizer_path, do_lower_case=True)
        
        # load COCO annotations, classes, class <-> id mappings
        cats = []
        cat_path = osp.join(self.root_dir, 'vg_obj_categories.txt')
        print('Loading categories from "%s"' % cat_path)
        with open(cat_path, 'r') as f:
            for line in f:
                cats.append(line.strip())
        self.classes = tuple(['<pad>', '<sos>', '<eos>'] + [c for c in cats])
        self.class_to_ind = dict(zip(self.classes, range(len(self.classes))))

        splits_json = osp.join(self.root_dir, 'vg_splits.json')
        with open(splits_json, 'r') as f:
            self.splits_index = json.load(f)
        self.image_index = sorted(self.splits_index[self.split])

        dataset_json = osp.join(self.root_dir, 'vg_for_layout.json')
        with open(dataset_json, 'r') as f:
            self.dataset = json.load(f)
        self.image_id_to_data = {d['image_id']: d for d in self.dataset}

        # scene database
        self.scenedb = self.gt_scenedb()

        # filter and sort
        # if self.split != 'test':
        #     self.filter_scenedb()
        #     self.scenedb = [self.sort_objects(x) for x in self.scenedb]

        # self.filter_scenedb()
        self.scenedb = [self.sort_objects(x) for x in self.scenedb]
        print('Loaded {} data for {}'.format(len(self.scenedb), self.split))

        # grid mapping
        self.loc_map = CocoLocationMap(self.cfg)
        self.trans_map = CocoTransformationMap(self.cfg)
        colormap = create_colormap(len(self.classes))
        color_inds = np.random.permutation(range(len(colormap)))
        self.colormap = [colormap[x] for x in color_inds]
        print(self.max_len)
        print(self.min_len)

  
    def __len__(self):
        return len(self.scenedb)
    
    def __getitem__(self, idx):
        entry = {}
        scene = self.scenedb[idx].copy()
        entry['scene_idx'] = int(idx) 
        entry['image_idx'] = int(scene['img_idx'])
        entry['width'] = scene['width']
        entry['height'] = scene['height']

        ###################################################################
        ## Sentence
        ###################################################################
        group_sents = scene['captions']
        if self.split == 'train' and self.cfg.sent_group < 0:
            cid = np.random.randint(0, len(group_sents))
        else:
            cid = self.cfg.sent_group

        sentence = group_sents[cid] 
        entry['sentence'] = sentence 

        # group_bert_token = scene['tokenize_captions']
        # bert_token = group_bert_token[cid]                
        # bert_inds, bert_msks = self.pad_sequence(bert_token, 
        #     self.cfg.max_input_length, self.cfg.PAD_idx, None, None, None)
        # entry['bert_inds'] = bert_inds
        # entry['bert_lens'] = np.sum(bert_msks).astype(np.int32)
        entry['bert_inds'] = scene['bert_inds'][cid]
        entry['bert_lens'] = scene['bert_lens'][cid]

        ###################################################################
        ## Indices
        ###################################################################
        # Word indices
        entry['word_lens'] = len(entry['sentence'])

        # Output inds
        out_inds, out_msks, boxes, box_msks = self.scene_to_output_inds(scene)  
        entry['out_inds'] = out_inds
        entry['out_msks'] = out_msks
        entry['boxes'] = boxes
        entry['box_msks'] = box_msks
        entry['obj_cnt'] = sum(box_msks[:,0]).astype(np.int32)

        gt_fg_inds = deepcopy(out_inds[:,0]).astype(np.int32).flatten().tolist()
        gt_fg_inds = [self.cfg.SOS_idx] + gt_fg_inds
        gt_fg_inds = np.array(gt_fg_inds)
        entry['fg_inds'] = gt_fg_inds

        ###################################################################
        ## Images and Layouts
        ###################################################################
        entry['color_path'] = scene['img_path']
        vols = self.render_vols(out_inds, return_sequence=True)
        pad_vol = np.zeros_like(vols[-1])
        entry['background'], _ = \
            self.pad_sequence(vols, self.cfg.max_output_length, pad_vol, pad_vol, None, 0.0)

        ###################################################################
        ## Transformation
        ###################################################################
        if self.transform:
            entry = self.transform(entry)
        return entry

    def pad_bert(self, inputs, max_length, pad_val):
        seq = inputs[:max_length]
        seq_len = len(seq)
        num_padding = max_length - len(seq)

        for i in range(num_padding):
            if isinstance(pad_val, np.ndarray):
                seq.append(pad_val.copy())
            else:
                seq.append(pad_val)
        return np.array(seq).astype(np.float32), seq_len

    def load_vg_annotation(self, index):
        """
        Loads COCO bounding boxes and caption annotations.
        Crowd instances are ignored.
        """
        im_ann = self.image_id_to_data[index]
        width  = im_ann['width']; height = im_ann['height']

        #######################################################################
        ## Make the image square
        #######################################################################
        max_dim = max(width, height)
        offset_x = 0 # int(0.5 * (max_dim - width))
        offset_y = max_dim - height # int(0.5 * (max_dim - height))

        #######################################################################
        ## Objects that are outside crowd regions
        #######################################################################

        objIds = im_ann['objIds']
        objs   = im_ann['clses']
        caps   = im_ann['captions']
        boxes_ = im_ann['boxes']

        #######################################################################
        ## Main information: normalized bounding boxes and class indices
        #######################################################################
        boxes = []
        clses = []
        areas = [] # for small objects filtering
        #######################################################################

        #######################################################################
        ## Lookup table to map from COCO category ids to our internal class indices
        ## Real object categories start from index 3
        #######################################################################
        start_idx = self.cfg.EOS_idx + 1
        # coco_cat_id_to_class_ind = dict([(self.class_to_coco_cat_id[cls],
        #                                   self.class_to_ind[cls])
        #                                   for cls in self.classes[start_idx:]])

        #######################################################################
        ## For each object
        #######################################################################
        for i in range(len(objs)):
            obj = objs[i]
            box = boxes_[i]
            #######################################################################
            ## Normalized bounding box
            #######################################################################
            x1 = np.max((0, box[0]))
            y1 = np.max((0, box[1]))
            x2 = np.min((width - 1, x1 + np.max((0, box[2] - 1))))
            y2 = np.min((height - 1, y1 + np.max((0, box[3] - 1))))

            assert(x2 >= x1 and y2 >= y1)
            # try:
            #     assert(x2 >= x1 and y2 >= y1)
            # except BaseException as e:
            #     print(width,height,box,x1,x2,y1,y2)
            # area = (x2 - x1 + 1.0) * (y2 - y1 + 1.0)

            x1 += offset_x; y1 += offset_y
            x2 += offset_x; y2 += offset_y
            cx = 0.5 * (x1 + x2); cy = 0.5 * (y1 + y2)
            nw = x2 - x1 + 1.0; nh = y2 - y1 + 1.0

            bb = np.array([cx, cy, nw, nh], dtype=np.float32)/max_dim
            #######################################################################
            ## Class index
            #######################################################################
            # cls = coco_cat_id_to_class_ind[obj['category_id']]
            cls = self.class_to_ind[obj]
            area = bb[2] * bb[3]

            boxes.append(bb)
            clses.append(cls)
            areas.append(area)

        captions = [x.lower() for x in caps]
        # tokenize_captions = []
        bert_inds, bert_lens = [], []

        for caption in captions:
            tokenized_text = self.tokenizer.tokenize(caption)
            tokens = []
            tokens.append("[CLS]")
            for token in tokenized_text[:self.cfg.max_input_length-2]:
                tokens.append(token)
            tokens.append("[SEP]")
            indexed_tokens = self.tokenizer.convert_tokens_to_ids(tokens)

            self.max_len = max(self.max_len, len(objIds))
            self.min_len = min(self.min_len, len(objIds))

            # tokenize_captions.append(indexed_tokens)
            bert_ind, bert_len = self.pad_bert(indexed_tokens, self.cfg.max_input_length, self.cfg.PAD_idx)
            bert_inds.append(bert_ind)
            bert_lens.append(bert_len)


        return  {
            'img_idx'  : index,
            'captions' : captions,
            'boxes'    : np.array(boxes),
            'clses'    : np.array(clses),
            'areas'    : np.array(areas),
            'objIds'   : np.array(objIds),
            'img_path' : im_ann['image_path'],
            'width'    : width,
            'height'   : height,
            # 'tokenize_captions' : tokenize_captions
            'bert_inds': np.array(bert_inds),
            'bert_lens': np.array(bert_lens),
            'obj_cnt'  : len(objIds)
        }

    def gt_scenedb(self):
        if osp.exists(self.cache_file):
            scenedb = pickle_load(self.cache_file)
            print('gt roidb loaded from {}'.format(self.cache_file))
            return scenedb
        scenedb = [self.load_vg_annotation(index) for index in tqdm(self.image_index)]
        pickle_save(self.cache_file, scenedb)
        print('wrote gt roidb to {}'.format(self.cache_file))
        return scenedb
    
    def encode_sentence(self, sentence):
     
        tokenized_text = self.tokenizer.tokenize(sentence)
        tokens = []
        mask = []
        tokens.append("[CLS]")
        mask.append(1)
        for token in tokenized_text[:self.cfg.max_input_length-2]:
            tokens.append(token)
            mask.append(1)
        tokens.append("[SEP]")
        mask.append(1)
        indexed_tokens = self.tokenizer.convert_tokens_to_ids(tokens)
        # tokenize_captions.append(indexed_tokens)
        bert_ind, bert_len = self.pad_bert(indexed_tokens, self.cfg.max_input_length, self.cfg.PAD_idx)
        return bert_ind.astype(np.int32), np.array([bert_len]).astype(np.int32)

        


class test_layout_vg_bert(layout_vg_bert):
    def __init__(self, config, split=None, transform=None):
        self.cfg = config
        self.tokenizer_path = 'bert-base' if self.cfg.bert_path=='' else os.path.join(self.cfg.bert_path, 'bert-base-uncased-vocab.txt')
        
        self.split = split
        if split is not None:
            self.name = 'layout_vg' + '_' + split
        else:
            self.name = 'layout_vg'

        self.max_len = 0
        self.min_len = 500
        self.transform = transform
        self.root_dir  = osp.join(config.data_dir, 'vg')
        self.cache_dir = osp.abspath(osp.join(config.data_dir, 'caches'))
        maybe_create(self.cache_dir)
        self.cache_file = osp.join(self.cache_dir, 'test_bert_layout_vg_' + self.split + '.pkl')
        
        self.tokenizer = BertTokenizer.from_pretrained(self.tokenizer_path, do_lower_case=True)
        
        # load COCO annotations, classes, class <-> id mappings
        cats = []
        cat_path = osp.join(self.root_dir, 'vg_obj_categories.txt')
        print('Loading categories from "%s"' % cat_path)
        with open(cat_path, 'r') as f:
            for line in f:
                cats.append(line.strip())
        self.classes = tuple(['<pad>', '<sos>', '<eos>'] + [c for c in cats])
        self.class_to_ind = dict(zip(self.classes, range(len(self.classes))))

        splits_json = osp.join(self.root_dir, 'vg_splits.json')
        with open(splits_json, 'r') as f:
            self.splits_index = json.load(f)
        self.image_index = sorted(self.splits_index[self.split])

        dataset_json = osp.join(self.root_dir, 'vg_for_layout.json')
        with open(dataset_json, 'r') as f:
            self.dataset = json.load(f)
        self.image_id_to_data = {d['image_id']: d for d in self.dataset}

        # scene database
        self.scenedb = self.gt_scenedb()

        # filter and sort
        # if self.split != 'test':
        #     self.filter_scenedb()
        #     self.scenedb = [self.sort_objects(x) for x in self.scenedb]

        # self.filter_scenedb()
        self.scenedb = [self.sort_objects(x) for x in self.scenedb]
        print('Loaded {} data for {}'.format(len(self.scenedb), self.split))

        # grid mapping
        self.loc_map = CocoLocationMap(self.cfg)
        self.trans_map = CocoTransformationMap(self.cfg)
        colormap = create_colormap(len(self.classes))
        color_inds = np.random.permutation(range(len(colormap)))
        self.colormap = [colormap[x] for x in color_inds]

    def gt_scenedb(self):
        if osp.exists(self.cache_file):
            scenedb = pickle_load(self.cache_file)
            print('gt roidb loaded from {}'.format(self.cache_file))
            return scenedb
        scenedb = [self.load_vg_annotation(index) for index in tqdm(self.image_index[:50])]
        pickle_save(self.cache_file, scenedb)
        print('wrote gt roidb to {}'.format(self.cache_file))
        return scenedb
