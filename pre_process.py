import librosa
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
import matplotlib.pyplot as plt
import os
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from loguru import logger
from pathlib import Path
from rich import print
import sys
import random
import noisereduce as nr
from multiprocessing import Pool, cpu_count
from audiomentations import Compose, AddGaussianNoise, TimeStretch, PitchShift, Shift
from some_tools import add_tensorboard_image
from torch.utils.tensorboard import SummaryWriter
import uuid
from sklearn.metrics.pairwise import cosine_similarity
from scipy.interpolate import interp1d

# 定义三个类别
CLSAA = [0, 1, 2]  # 0 松 1 正常 2 紧
CLSAA_DICT = {0: "松", 1: "正常", 2: "紧"}
# 插值目标长度
TARGET_LENGTH = 150

# 保存日志文件,以追加模式，每天一个文件
logger.add("logs/{time:YYYY-MM-DD-HH}.log", rotation="1 day", encoding="utf-8")


# 归一化函数
def normalize_mfccs(mfccs) -> np.ndarray:
    mfccs_mean = mfccs.mean(axis=1, keepdims=True)
    mfccs_std = mfccs.std(axis=1, keepdims=True)
    normalized_mfccs = (mfccs - mfccs_mean) / mfccs_std
    return normalized_mfccs


# 计算余弦相似度
# mfccs: MFCC特征
# threshold: 阈值
def find_similar_segments(mfccs, threshold=0.99):
    # 计算余弦相似度矩阵
    sim_matrix = cosine_similarity(mfccs.T)
    similar_pairs = []

    for i in range(sim_matrix.shape[0]):
        for j in range(i + 1, sim_matrix.shape[1]):
            if sim_matrix[i, j] > threshold:
                similar_pairs.append((i, j))

    return similar_pairs


# 插值函数, 统一shape
def interpolate_mfcc(mfccs, target_length):
    n_mfcc, original_length = mfccs.shape
    interpolation_function = interp1d(
        np.arange(original_length),
        mfccs,
        # kind="linear",
        kind="nearest",
        axis=1,
        fill_value="extrapolate",
    )
    new_index = np.linspace(0, original_length - 1, target_length)
    new_mfccs = interpolation_function(new_index)
    return new_mfccs

    ##'linear': 线性插值。这是最简单的插值类型，它只是在两个点之间画一条直线。
    ## 'nearest': 最近邻插值。这种类型的插值选择最近的点，而不尝试插值。
    ## 'zero': 零阶插值。这种类型的插值在每一对点之间画一条水平线。
    ## 'slinear': 一阶样条插值。这种类型的插值使用一阶样条进行插值。
    ## 'quadratic': 二阶样条插值。这种类型的插值使用二阶样条进行插值。
    ## 'cubic': 三阶样条插值。这种类型的插值使用三阶样条进行插值。


# 音频加载和特征提取函数
# sr: 采样率22050
# n_mfcc: MFCC的数量
def load_audio_features(
    file_path: str,
    sr: int = 22050,
    n_mfcc: int = 40,
    augment: bool = False,
    # target_length: int = 431,
) -> np.ndarray:

    audio, sr = librosa.load(file_path, sr=sr)

    mfccs = librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=n_mfcc)

    # 去除重复的帧

    similar_pairs = find_similar_segments(mfccs)

    # 去除所有配对的相似帧中一个
    mfccs = np.delete(mfccs, [pair[0] for pair in similar_pairs], axis=1)

    return mfccs


# 读取文件夹下所有音频文件
def read_audio_files(folder_path: str) -> list[str]:

    audio_paths = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.endswith(".wav"):
                audio_paths.append(os.path.join(root, file))
    return audio_paths


# 数据集类
class AudioDataset(Dataset):
    def __init__(self, audio_paths, labels):

        self.audio_paths: list[str] = audio_paths
        self.labels: list[int] = labels

        self.label_list = [
            torch.tensor(label, dtype=torch.long) for label in self.labels
        ]

        with Pool(int(cpu_count())) as p:
            self.feature_list = p.starmap(
                load_audio_features,
                [(path, None, 40, True) for path in self.audio_paths],
            )

        # 计算目标长度,所有音频文件的MFCC特征的时间帧的最大值
        # target_length = np.max([mfcc.shape[1] for mfcc in self.feature_list]).astype(
        #     int
        # )

        target_length = TARGET_LENGTH  # 固定长度

        logger.info(f"target_length: {target_length}")

        # 插值所有 MFCC 矩阵到这个目标长度
        self.feature_list = [
            interpolate_mfcc(mfcc, target_length) for mfcc in self.feature_list
        ]

        logger.success("插值完成")

        # 转置
        self.feature_list = [mfcc.T for mfcc in self.feature_list]

        logger.success("转置完成")

        # 转换为张量

        self.feature_list = [
            torch.tensor(mfcc, dtype=torch.float32) for mfcc in self.feature_list
        ]

        logger.success("转换为张量完成")

    def __len__(self) -> int:
        assert len(self.audio_paths) == len(self.labels), "数据和标签数量不一致"
        return len(self.audio_paths)

    def __getitem__(self, idx) -> tuple[torch.Tensor, torch.Tensor]:

        return self.feature_list[idx], self.label_list[idx]


class AudioClassifier(nn.Module):
    def __init__(self):
        super(AudioClassifier, self).__init__()
        self.lstm = nn.LSTM(
            input_size=40,
            hidden_size=64,
            num_layers=2,
            batch_first=True,
            bidirectional=True,
        )
        self.dropout = nn.Dropout(0.5)  # 添加Dropout层
        self.fc = nn.Linear(64 * 2, len(CLSAA))  # 输出层，双向LSTM的输出维度

    def forward(self, x):
        _, (h_n, _) = self.lstm(x)
        # 由于使用了双向LSTM，需要将前向和后向的隐藏状态拼接起来
        h_n = torch.cat((h_n[-2, :, :], h_n[-1, :, :]), dim=1)
        x = self.dropout(h_n)  # 应用dropout
        x = self.fc(x)
        return F.log_softmax(x, dim=1)


# 训练模型函数
# model: 模型
# dataloader: 数据加载器
# criterion: 损失函数
# optimizer: 优化器
# num_epochs: 训练的轮数
def train_model(
    model,
    dataloader,
    criterion=nn.CrossEntropyLoss(),  # 使用交叉熵损失函数
    optimizer=None,
    num_epochs=30,
    draw_loss=False,
):
    if not optimizer:
        optimizer = optim.Adam(model.parameters(), lr=0.001)  # 使用Adam优化器
    loss_list = []
    for epoch in range(num_epochs):
        total_loss = 0
        # loss_list = []
        for i, (features, labels) in enumerate(dataloader):
            optimizer.zero_grad()  # 梯度清零
            outputs = model(features)  # 前向传播
            # print("outputs: ", outputs.tolist())
            loss = criterion(outputs, labels)  # 计算损失
            loss_list.append(loss.item())
            total_loss += loss.item()
            loss.backward()  # 反向传播
            optimizer.step()

            if (i + 1) % 5 == 0:
                logger.info(
                    f"epoch [{epoch+1}/{num_epochs}], step [{i+1}/{len(dataloader)}], loss: {loss.item():.4f}"
                )

        avg_loss = total_loss / len(dataloader)
        logger.info(f"epoch [{epoch+1}/{num_epochs}], avg_loss: {avg_loss:.4f}")

    logger.success("训练完成")

    if draw_loss:
        # plt.plot(loss_list, label="loss")
        # plt.legend()
        # plt.xlabel("Step")
        # plt.ylabel("Loss")
        # plt.title("Training Loss")
        # logger.warning("close the plot window to continue...")
        # plt.show()
        # 创建 SummaryWriter 对象

        writer = SummaryWriter(f"logs{str(uuid.uuid4())}")

        # 将损失值记录到 TensorBoard
        for step, loss in enumerate(loss_list):
            writer.add_scalar("Loss", loss, step)

        # 关闭 SummaryWriter
        writer.close()


# 一个测试加载器
def test_model(model, dataloader) -> float:
    model.eval()  # 设置模型为评估模式
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    correct = 0
    total = 0
    with torch.no_grad():
        # 遍历数据加载器 ，获取特征和标签 ，labels是真实标签
        for features, labels in dataloader:
            features = features.to(device)
            labels = labels.to(device)

            # 获取模型的输出
            outputs = model(features)

            # 获取预测结果
            _, predicted = torch.max(outputs.data, 1)

            assert predicted.shape == labels.shape, "预测结果和标签数量不一致"

            total += labels.size(0)

            predicted_class = [
                CLSAA_DICT[predicted[i].item()] for i in range(len(predicted))
            ]

            labels_class = [CLSAA_DICT[labels[i].item()] for i in range(len(labels))]

            print(
                "predicted_class: ",
                predicted_class,
            )

            print(
                "labels_class: ",
                labels_class,
            )

            if all(p == l for p, l in zip(predicted_class, labels_class)):
                logger.success("all equal")
            else:
                logger.error("not equal")

            correct += (predicted == labels).sum().item()

    accuracy = 100 * correct / total
    logger.debug(f"Accuracy on the test set: {accuracy:.2f}%")

    return accuracy


# 给定音频路径获取类型
# def get_audio_type(model, audio_path: str) -> str:
#     model.eval()
#     device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#     model.to(device)
#     features = load_audio_features(audio_path)
#     outputs = model(features)
#     _, predicted = torch.max(outputs.data, 1)
#     return CLSAA_DICT[predicted.item()]
