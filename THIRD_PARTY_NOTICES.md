# Third-party notices

Mosaic Photo-Editor is MIT licensed (see [LICENSE](LICENSE)). It uses the following work by others,
each under its own licence. Packages are installed from PyPI by `scripts/install.sh`; they are not
copied into this repository.

## Included in this repository

| Component | Author | Licence |
|---|---|---|
| YuNet face detection model (`mosaic/models/face_detection_yunet_2023mar.onnx`) | Shiqi Yu, Yuantao Feng et al., [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT |

## Downloaded on first use (AI features)

| Model | Author | Licence | Source |
|---|---|---|---|
| BiRefNet-portrait | Peng Zheng et al. | MIT | [huggingface.co/ZhengPeng7/BiRefNet-portrait](https://huggingface.co/ZhengPeng7/BiRefNet-portrait) |
| LaMa, big-lama weights (TorchScript export) | Roman Suvorov et al., Samsung AI Center | Apache-2.0 | [advimman/lama](https://github.com/advimman/lama), export from [Sanster/models](https://github.com/Sanster/models) |

## Python packages

| Package | Licence |
|---|---|
| Streamlit | Apache-2.0 |
| Pillow | MIT-CMU (HPND) |
| pillow-heif | BSD-3-Clause (bundles libheif, LGPL-3.0) |
| NumPy | BSD-3-Clause |
| OpenCV (opencv-python-headless) | Apache-2.0 (package MIT) |
| streamlit-image-coordinates | MIT |
| Send2Trash | BSD-3-Clause |
| pywebview | BSD-3-Clause |
| PyTorch, torchvision | BSD-3-Clause |
| Hugging Face Transformers | Apache-2.0 |
| timm | Apache-2.0 |
| Kornia | Apache-2.0 |
| einops | MIT |
| Requests | Apache-2.0 |

If you notice a missing or incorrect notice, please open an issue.
