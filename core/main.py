import collections
import configparser
import itertools
import notifypy
import os
import re
import sys
import tempfile
import time
import threading
import traceback
import typing
import webbrowser
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtGui import QIcon
from PySide6.QtGui import QPixmap
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QButtonGroup
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QMainWindow
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QProgressBar
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QRadioButton
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QTabWidget
from PySide6.QtWidgets import QTextBrowser
from PySide6.QtWidgets import QTreeWidget
from PySide6.QtWidgets import QTreeWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

import define
import param
import task

# [error] exceeds limit of 178956970 pixels，能否扩大图片像素的限制呢，比如10亿像素。 · Issue #34 · TransparentLC/realesrgan-gui
# https://github.com/TransparentLC/realesrgan-gui/issues/34
# https://github.com/python-pillow/Pillow/blob/e3cca4298011a4e74d6f42b4cfe5a0610d3c79a9/src/PIL/Image.py#L3140
Image.MAX_IMAGE_PIXELS = None

# “开始/继续”按钮的高亮样式（对应 Sun Valley 的 Accent.TButton）
ACCENT_QSS = '''
QPushButton[accent="true"] {
    background-color: #0078d4;
    color: #ffffff;
    border: 1px solid #0078d4;
    border-radius: 4px;
    padding: 6px 16px;
}
QPushButton[accent="true"]:hover {
    background-color: #1684d8;
}
QPushButton[accent="true"]:pressed {
    background-color: #006cc0;
}
'''
# 文件列表行内进度条的状态着色（失败红 / 停止灰）
ITEM_STATE_QSS = '''
QProgressBar[state="failed"]::chunk {
    background-color: #d13438;
}
QProgressBar[state="stopped"]::chunk {
    background-color: #8a8886;
}
'''

# 文件列表行状态 -> 显示文案
ITEM_STATE_LABELS = {
    'waiting': '等待中',
    'processing': '处理中',
    'done': '完成',
    'failed': '失败',
    'stopped': '已停止',
    'skipped': '未处理',
}

class REGUIApp(QMainWindow):
    # 工作线程通过信号把日志/回调投递到 GUI 线程，Qt 不允许跨线程直接操作界面
    sigOutput = Signal(str)
    sigComplete = Signal(bool)
    sigFail = Signal(str)
    sigFinally = Signal()
    # 文件列表行状态/进度（itemId, state）与（itemId, 0~1 的进度）
    sigItemState = Signal(int, str)
    sigItemProgress = Signal(int, float)

    def __init__(self, config: configparser.ConfigParser, models: list[str]):
        super().__init__()
        self.models = models
        for m in (
            'realesrgan-x4plus',
            'realesrgan-x4plus-anime',
        )[::-1]:
            try:
                self.models.insert(0, self.models.pop(self.models.index(m)))
            except ValueError:
                pass
        self.modelFactors: dict[str, int] = {}
        for m in self.models:
            self.modelFactors[m] = 4
            if s := re.search(r'(\d+)x|x(\d+)', m):
                self.modelFactors[m] = int(s.group(1) or s.group(2))

        self.downsample = (
            ('Lanczos', Image.Resampling.LANCZOS),
            ('Bicubic', Image.Resampling.BICUBIC),
            ('Hamming', Image.Resampling.HAMMING),
            ('Bilinear', Image.Resampling.BILINEAR),
            ('Box', Image.Resampling.BOX),
            ('Nearest', Image.Resampling.NEAREST),
        )
        self.tileSize = (0, 32, 64, 128, 256, 512, 1024, 2048, 4096)

        self.config = config

        self.logPath = os.path.join(define.APP_PATH, 'output.log')
        self.logFile: typing.IO = None
        # 任务共享进度：当前文件进度（0~1）/已完成文件数/总文件数（由 task.py 写入）
        self.progressValue: list[int | float] = [0, 0, 1]
        # 处理状态
        self.processing = False
        self.processingPaused = False
        # 控制是否暂停
        self.pauseEvent = threading.Event()
        # 控制是否停止（取消）处理
        self.cancelEvent = threading.Event()

        self.sigOutput.connect(self.writeToOutput, Qt.ConnectionType.QueuedConnection)
        self.sigComplete.connect(self.onTaskComplete, Qt.ConnectionType.QueuedConnection)
        self.sigFail.connect(self.onTaskFail, Qt.ConnectionType.QueuedConnection)
        self.sigFinally.connect(self.onTaskFinally, Qt.ConnectionType.QueuedConnection)
        self.sigItemState.connect(self.onItemState, Qt.ConnectionType.QueuedConnection)
        self.sigItemProgress.connect(self.onItemProgress, Qt.ConnectionType.QueuedConnection)

        self.setupWidgets()

        if self.config['Config'].get('ModelDir'):
            self.writeToOutput(f"Using custom model dir: {self.config['Config'].get('ModelDir')}\n")
        if self.config['Config'].get('Upscaler'):
            self.writeToOutput(f"Using custom upscaler executable: {self.config['Config'].get('Upscaler')}\nThe executable (and models) may be incompatible with Real-ESRGAN-ncnn-vulkan. Use at your own risk!\n")

    def setupWidgets(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        centralLayout = QVBoxLayout(central)
        centralLayout.setContentsMargins(5, 5, 5, 5)

        self.notebookConfig = QTabWidget(self)
        centralLayout.addWidget(self.notebookConfig)

        # ---- 基本配置 ----
        self.frameBasicConfig = QWidget(self)
        basicLayout = QVBoxLayout(self.frameBasicConfig)
        basicLayout.setContentsMargins(5, 5, 5, 5)

        self.labelInputPath = QLabel('输入（可多选图片文件）', self.frameBasicConfig)
        basicLayout.addWidget(self.labelInputPath)
        inputRow = QHBoxLayout()
        self.entryInputPath = QLineEdit(self.frameBasicConfig)
        inputRow.addWidget(self.entryInputPath, 1)
        self.buttonInputPath = QPushButton('浏览', self.frameBasicConfig)
        self.buttonInputPath.clicked.connect(self.buttonInputPath_click)
        inputRow.addWidget(self.buttonInputPath)
        basicLayout.addLayout(inputRow)

        self.labelOutputPath = QLabel('输出目录', self.frameBasicConfig)
        basicLayout.addWidget(self.labelOutputPath)
        outputRow = QHBoxLayout()
        self.entryOutputPath = QLineEdit(self.frameBasicConfig)
        outputRow.addWidget(self.entryOutputPath, 1)
        self.buttonOutputPath = QPushButton('浏览', self.frameBasicConfig)
        self.buttonOutputPath.clicked.connect(self.buttonOutputPath_click)
        outputRow.addWidget(self.buttonOutputPath)
        basicLayout.addLayout(outputRow)

        bottomRow = QHBoxLayout()
        frameResize = QWidget(self.frameBasicConfig)
        resizeLayout = QGridLayout(frameResize)
        resizeLayout.setContentsMargins(0, 0, 0, 0)
        self.labelResizeMode = QLabel('放大尺寸计算方式', frameResize)
        resizeLayout.addWidget(self.labelResizeMode, 0, 0, 1, 2)
        self.resizeModeGroup = QButtonGroup(self)
        self.radioResizeRatio = QRadioButton('倍率', frameResize)
        self.spinResizeRatio = QSpinBox(frameResize, minimum=2, maximum=16)
        resizeLayout.addWidget(self.radioResizeRatio, 1, 0)
        resizeLayout.addWidget(self.spinResizeRatio, 1, 1)
        self.radioResizeWidth = QRadioButton('宽度', frameResize)
        self.spinResizeWidth = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeWidth, 2, 0)
        resizeLayout.addWidget(self.spinResizeWidth, 2, 1)
        self.radioResizeHeight = QRadioButton('高度', frameResize)
        self.spinResizeHeight = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeHeight, 3, 0)
        resizeLayout.addWidget(self.spinResizeHeight, 3, 1)
        self.radioResizeLongestSide = QRadioButton('较长边', frameResize)
        self.spinResizeLongestSide = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeLongestSide, 4, 0)
        resizeLayout.addWidget(self.spinResizeLongestSide, 4, 1)
        self.radioResizeShortestSide = QRadioButton('较短边', frameResize)
        self.spinResizeShortestSide = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeShortestSide, 5, 0)
        resizeLayout.addWidget(self.spinResizeShortestSide, 5, 1)
        for radio, mode in (
            (self.radioResizeRatio, param.ResizeMode.RATIO),
            (self.radioResizeWidth, param.ResizeMode.WIDTH),
            (self.radioResizeHeight, param.ResizeMode.HEIGHT),
            (self.radioResizeLongestSide, param.ResizeMode.LONGEST_SIDE),
            (self.radioResizeShortestSide, param.ResizeMode.SHORTEST_SIDE),
        ):
            self.resizeModeGroup.addButton(radio, int(mode))
        bottomRow.addWidget(frameResize, 1)

        rightColumn = QVBoxLayout()
        self.labelUsedModel = QLabel('模型', self.frameBasicConfig)
        rightColumn.addWidget(self.labelUsedModel)
        self.comboModel = QComboBox(self.frameBasicConfig)
        self.comboModel.addItems(self.models)
        rightColumn.addWidget(self.comboModel)
        rightColumn.addStretch(1)
        self.buttonStop = QPushButton('停止', self.frameBasicConfig)
        self.buttonStop.setEnabled(False)
        self.buttonStop.clicked.connect(self.buttonStop_click)
        rightColumn.addWidget(self.buttonStop, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.buttonProcess = QPushButton(self.frameBasicConfig)
        self.buttonProcess.clicked.connect(self.buttonProcess_click)
        rightColumn.addWidget(self.buttonProcess, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        bottomRow.addLayout(rightColumn, 1)
        basicLayout.addLayout(bottomRow)

        # ---- 高级配置 ----
        self.frameAdvancedConfig = QWidget(self)
        advancedLayout = QHBoxLayout(self.frameAdvancedConfig)
        advancedLayout.setContentsMargins(5, 5, 5, 5)

        leftColumn = QVBoxLayout()
        downTileRow = QHBoxLayout()
        downColumn = QVBoxLayout()
        self.labelDownsampleMode = QLabel('降采样方式', self.frameAdvancedConfig)
        downColumn.addWidget(self.labelDownsampleMode)
        self.comboDownsample = QComboBox(self.frameAdvancedConfig)
        self.comboDownsample.addItems(tuple(x[0] for x in self.downsample))
        downColumn.addWidget(self.comboDownsample)
        downTileRow.addLayout(downColumn, 1)
        tileColumn = QVBoxLayout()
        self.labelTileSize = QLabel('拆分大小', self.frameAdvancedConfig)
        tileColumn.addWidget(self.labelTileSize)
        self.comboTileSize = QComboBox(self.frameAdvancedConfig)
        # 首项「自动决定」对应 tileSize[0] = 0，其余为固定拆分尺寸
        self.comboTileSize.addItem('自动决定')
        self.comboTileSize.addItems(tuple(str(x) for x in self.tileSize[1:]))
        tileColumn.addWidget(self.comboTileSize)
        downTileRow.addLayout(tileColumn, 1)
        leftColumn.addLayout(downTileRow)

        self.labelUsedGPUID = QLabel('使用的 GPU ID（-1 为自动选择）', self.frameAdvancedConfig)
        leftColumn.addWidget(self.labelUsedGPUID)
        self.spinGPUID = QSpinBox(self.frameAdvancedConfig, minimum=-1, maximum=7)
        leftColumn.addWidget(self.spinGPUID)
        self.labelLossyModeQuality = QLabel('有损压缩质量（0-100）', self.frameAdvancedConfig)
        leftColumn.addWidget(self.labelLossyModeQuality)
        self.spinLossyQuality = QSpinBox(self.frameAdvancedConfig, minimum=0, maximum=100, singleStep=5)
        leftColumn.addWidget(self.spinLossyQuality)
        self.labelCustomCommand = QLabel('自定义压缩/后期处理命令', self.frameAdvancedConfig)
        leftColumn.addWidget(self.labelCustomCommand)
        self.entryCustomCommand = QLineEdit(self.frameAdvancedConfig)
        leftColumn.addWidget(self.entryCustomCommand)
        leftColumn.addStretch(1)
        advancedLayout.addLayout(leftColumn, 1)

        rightColumnAdv = QVBoxLayout()
        self.checkDarkMode = QCheckBox('深色模式（立即生效，重启后保持）', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkDarkMode)
        self.checkUseWebP = QCheckBox('优先保存为无损 WebP', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkUseWebP)
        self.checkUseTTA = QCheckBox('使用 TTA 模式（速度大幅下降，稍微提高质量）', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkUseTTA)
        self.checkOptimizeGIF = QCheckBox('针对 GIF 的透明色进行额外处理（实验性功能）', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkOptimizeGIF)
        self.checkLossyMode = QCheckBox('使用有损压缩（JPEG/WebP）', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkLossyMode)
        self.checkIgnoreError = QCheckBox('在批处理过程中忽略错误并继续处理', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkIgnoreError)
        self.checkPreupscale = QCheckBox('尝试预先使用常规算法放大', self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkPreupscale)
        rightColumnAdv.addStretch(1)
        advancedLayout.addLayout(rightColumnAdv, 3)

        # ---- 关于 ----
        self.frameAbout = QWidget(self)
        aboutLayout = QVBoxLayout(self.frameAbout)
        aboutLayout.addStretch(1)
        labelIcon = QLabel(self.frameAbout)
        labelIcon.setPixmap(QPixmap(os.path.join(define.BASE_PATH, 'asset', 'icons', 'icon-128px.png')))
        labelIcon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        aboutLayout.addWidget(labelIcon)
        labelTitle = QLabel(define.APP_TITLE, self.frameAbout)
        f = QFont(labelTitle.font())
        f.setPointSize(16)
        labelTitle.setFont(f)
        labelTitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        aboutLayout.addWidget(labelTitle)
        labelAuthor = QLabel('By TransparentLC' + (time.strftime("\nBuilt at %Y-%m-%d %H:%M:%S", time.localtime(define.BUILD_TIME)) if define.BUILD_TIME else ""), self.frameAbout)
        labelAuthor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        aboutLayout.addWidget(labelAuthor)
        aboutButtonGrid = QGridLayout()
        self.buttonViewREGUISource = QPushButton('查看源代码', self.frameAbout)
        self.buttonViewREGUISource.clicked.connect(lambda: webbrowser.open_new_tab('https://github.com/TransparentLC/realesrgan-gui'))
        aboutButtonGrid.addWidget(self.buttonViewREGUISource, 0, 0)
        self.buttonViewRESource = QPushButton('查看 Real-ESRGAN 介绍', self.frameAbout)
        self.buttonViewRESource.clicked.connect(lambda: webbrowser.open_new_tab('https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan'))
        aboutButtonGrid.addWidget(self.buttonViewRESource, 0, 1)
        self.buttonViewAdditionalModel = QPushButton('下载附加模型', self.frameAbout)
        self.buttonViewAdditionalModel.clicked.connect(lambda: webbrowser.open_new_tab('https://github.com/TransparentLC/realesrgan-gui/releases/tag/additional-models'))
        aboutButtonGrid.addWidget(self.buttonViewAdditionalModel, 1, 0)
        self.buttonViewDonatePage = QPushButton('捐赠支持开发者', self.frameAbout)
        self.buttonViewDonatePage.clicked.connect(lambda: webbrowser.open_new_tab('https://i.akarin.dev/donate/'))
        aboutButtonGrid.addWidget(self.buttonViewDonatePage, 1, 1)
        self.buttonModelGuide = QPushButton('模型选择指南', self.frameAbout)
        self.buttonModelGuide.clicked.connect(self.buttonModelGuide_click)
        aboutButtonGrid.addWidget(self.buttonModelGuide, 2, 0, 1, 2)
        aboutButtonRow = QHBoxLayout()
        aboutButtonRow.addStretch(1)
        aboutButtonRow.addLayout(aboutButtonGrid)
        aboutButtonRow.addStretch(1)
        aboutLayout.addLayout(aboutButtonRow)
        aboutLayout.addStretch(1)

        self.notebookConfig.addTab(self.frameBasicConfig, '基本设定')
        self.notebookConfig.addTab(self.frameAdvancedConfig, '高级设定')
        self.notebookConfig.addTab(self.frameAbout, '关于')

        # ---- 文件列表（逐项显示处理状态与进度） ----
        self.treeFiles = QTreeWidget(self)
        self.treeFiles.setColumnCount(3)
        self.treeFiles.setHeaderLabels(('文件', '状态', '进度'))
        self.treeFiles.setRootIsDecorated(False)
        self.treeFiles.setUniformRowHeights(True)
        self.treeFiles.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.treeFiles.setColumnWidth(1, 80)
        self.treeFiles.setColumnWidth(2, 160)
        centralLayout.addWidget(self.treeFiles, 1)

        # ---- 日志输出 ----
        self.textOutput = QPlainTextEdit(self)
        self.textOutput.setReadOnly(True)
        centralLayout.addWidget(self.textOutput, 1)

        # ---- 从配置恢复初始值 ----
        c = self.config['Config']
        self.spinResizeRatio.setValue(c.getint('ResizeRatio'))
        self.spinResizeWidth.setValue(c.getint('ResizeWidth'))
        self.spinResizeHeight.setValue(c.getint('ResizeHeight'))
        self.spinResizeLongestSide.setValue(c.getint('ResizeLongestSide'))
        self.spinResizeShortestSide.setValue(c.getint('ResizeShortestSide'))
        resizeModeButton = self.resizeModeGroup.button(c.getint('ResizeMode'))
        if resizeModeButton:
            resizeModeButton.setChecked(True)
        else:
            self.radioResizeRatio.setChecked(True)
        self.comboModel.setCurrentIndex(self.models.index(c.get('Model')) if c.get('Model') in self.models else 0)
        self.comboDownsample.setCurrentIndex(c.getint('DownsampleIndex'))
        self.spinGPUID.setValue(c.getint('GPUID'))
        self.spinLossyQuality.setValue(c.getint('LossyQuality'))
        self.checkUseWebP.setChecked(c.getboolean('UseWebP'))
        self.checkUseTTA.setChecked(c.getboolean('UseTTA'))
        self.checkOptimizeGIF.setChecked(c.getboolean('OptimizeGIF'))
        self.checkLossyMode.setChecked(c.getboolean('LossyMode'))
        self.checkIgnoreError.setChecked(c.getboolean('IgnoreError'))
        self.checkPreupscale.setChecked(c.getboolean('Preupscale'))
        # 主题：缺失或非法值时默认浅色
        self.checkDarkMode.setChecked(c.get('Theme', fallback='Light') == 'Dark')
        self.entryCustomCommand.setText(c.get('CustomCommand'))

        self.updateProcessButton()
        self.comboTileSize.setCurrentIndex(c.getint('TileSizeIndex'))

        # ---- 信号连接 ----
        # 初始值恢复完成后再连，避免 setChecked 时多触发一次 applyTheme
        self.checkDarkMode.toggled.connect(
            lambda checked: self.applyTheme('Dark' if checked else 'Light')
        )

        # 拖放输入已移除：QMainWindow 默认接受拖放，需显式关闭
        self.setAcceptDrops(False)

    def updateProcessButton(self):
        self.buttonProcess.setText(('继续' if self.processingPaused else '暂停') if self.processing else '开始')
        self.setButtonAccent(self.buttonProcess, not (self.processing and not self.processingPaused))
        self.buttonStop.setEnabled(self.processing and not self.cancelEvent.is_set())

    def buttonStop_click(self):
        if not self.processing or self.cancelEvent.is_set():
            return
        self.cancelEvent.set()
        # 暂停状态点停止 = 立即停止：先放行，让工作线程走到取消检查
        self.pauseEvent.set()
        self.processingPaused = False
        self.writeToOutput('正在停止…\n')
        self.updateProcessButton()

    def buttonModelGuide_click(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('模型选择指南')
        dialogLayout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        try:
            with open(os.path.join(define.BASE_PATH, '模型对比说明.md'), encoding='utf-8') as f:
                browser.setMarkdown(f.read())
        except OSError:
            browser.setPlainText(
                '未找到模型对比说明文件（模型对比说明.md）。\n'
                '如果是打包版本，可能是打包时遗漏了该文件，请重新下载完整版本。'
            )
        dialogLayout.addWidget(browser, 1)
        buttonClose = QPushButton('关闭', dialog)
        buttonClose.clicked.connect(dialog.accept)
        closeRow = QHBoxLayout()
        closeRow.addStretch(1)
        closeRow.addWidget(buttonClose)
        dialogLayout.addLayout(closeRow)
        dialog.resize(int(self.width() * 0.8), int(self.height() * 0.8))
        dialog.exec()

    @staticmethod
    def setButtonAccent(button: QPushButton, accent: bool):
        button.setProperty('accent', 'true' if accent else 'false')
        button.style().unpolish(button)
        button.style().polish(button)

    def applyTheme(self, theme: str):
        import qdarktheme
        qdarktheme.setup_theme(theme.lower() if theme else 'light', additional_qss=ACCENT_QSS + ITEM_STATE_QSS)
        # https://stackoverflow.com/questions/57124243/winforms-dark-title-bar-on-windows-10
        if sys.platform == 'win32':
            import ctypes
            match sys.getwindowsversion().build:
                case build if build >= 18985:
                    attribute = 20
                case build if build >= 17763:
                    attribute = 19
                case _:
                    attribute = None
            if attribute:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    attribute,
                    ctypes.byref(ctypes.c_int(theme == 'Dark')),
                    ctypes.sizeof(ctypes.c_int),
                )

    def closeEvent(self, event):
        self.saveConfig()
        super().closeEvent(event)

    def saveConfig(self):
        self.config['DEFAULT'] = {}
        self.config['Config'] = {
            'Upscaler': self.config['Config'].get('Upscaler') or '',
            'ModelDir': self.config['Config'].get('ModelDir') or '',
            'ResizeMode': self.resizeModeGroup.checkedId(),
            'ResizeRatio': self.spinResizeRatio.value(),
            'ResizeWidth': self.spinResizeWidth.value(),
            'ResizeHeight': self.spinResizeHeight.value(),
            'ResizeLongestSide': self.spinResizeLongestSide.value(),
            'ResizeShortestSide': self.spinResizeShortestSide.value(),
            'Model': self.comboModel.currentText(),
            'DownsampleIndex': self.comboDownsample.currentIndex(),
            'GPUID': self.spinGPUID.value(),
            'TileSizeIndex': self.comboTileSize.currentIndex(),
            'LossyQuality': self.spinLossyQuality.value(),
            'UseWebP': self.checkUseWebP.isChecked(),
            'UseTTA': self.checkUseTTA.isChecked(),
            'OptimizeGIF': self.checkOptimizeGIF.isChecked(),
            'LossyMode': self.checkLossyMode.isChecked(),
            'IgnoreError': self.checkIgnoreError.isChecked(),
            'Preupscale': self.checkPreupscale.isChecked(),
            'Theme': 'Dark' if self.checkDarkMode.isChecked() else 'Light',
            'CustomCommand': self.entryCustomCommand.text(),
        }
        with open(define.APP_CONFIG_PATH, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def buttonInputPath_click(self):
        p, _ = QFileDialog.getOpenFileNames(
            self,
            filter='Image files (*.jpg *.jpeg *.png *.gif *.webp *.tif *.tiff)',
        )
        if not p:
            return
        self.setInputPath(tuple(p))

    def buttonOutputPath_click(self):
        p = QFileDialog.getExistingDirectory(self)
        if not p:
            return
        self.entryOutputPath.setText(p)

    def buttonProcess_click(self):
        if self.processing:
            if self.processingPaused:
                self.processingPaused = False
                self.pauseEvent.set()
            else:
                self.processingPaused = True
                self.pauseEvent.clear()
                self.writeToOutput('Will pause after current task is completed.\n')
            self.updateProcessButton()
            return
        try:
            inputPaths = tuple(p.strip() for p in self.entryInputPath.text().split('|') if p.strip())
            outputDir = os.path.normpath(self.entryOutputPath.text().strip()) if self.entryOutputPath.text().strip() else ''
            if not inputPaths or not outputDir:
                return QMessageBox.warning(self, define.APP_TITLE, '请输入有效的输入路径和输出目录。')
            if not os.path.isdir(outputDir):
                return QMessageBox.warning(self, define.APP_TITLE, '输出路径不是已存在的目录。')

            initialConfigParams = self.getConfigParams()
            if initialConfigParams.resizeMode == param.ResizeMode.RATIO and initialConfigParams.resizeModeValue == 1:
                return QMessageBox.warning(self, define.APP_TITLE, '放大倍率必须为不小于 2 的整数。')

            # 输出文件名后缀由当前缩放模式生成
            match param.ResizeMode(self.resizeModeGroup.checkedId()):
                case param.ResizeMode.RATIO:
                    suffix = f'x{self.spinResizeRatio.value()}'
                case param.ResizeMode.WIDTH:
                    suffix = f'w{self.spinResizeWidth.value()}'
                case param.ResizeMode.HEIGHT:
                    suffix = f'h{self.spinResizeHeight.value()}'
                case param.ResizeMode.LONGEST_SIDE:
                    suffix = f'l{self.spinResizeLongestSide.value()}'
                case param.ResizeMode.SHORTEST_SIDE:
                    suffix = f's{self.spinResizeShortestSide.value()}'

            self.progressValue[0] = 0
            self.progressValue[1] = 0
            self.progressValue[2] = 0
            queue = collections.deque()
            # 重建文件列表：每个输入文件一行，行号即任务的 itemId
            self.treeFiles.clear()
            for inputPath in inputPaths:
                inputPath = os.path.normpath(inputPath)
                if not os.path.isfile(inputPath):
                    return QMessageBox.warning(self, define.APP_TITLE, '输入的文件不存在。')
                base, ext = os.path.splitext(os.path.basename(inputPath))
                if ext.lower() not in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.tif', '.tiff'}:
                    return QMessageBox.warning(self, define.APP_TITLE, '仅支持 JPEG、PNG、GIF、WebP 和 TIFF 格式的图片文件。')
                # 输出文件名 = 输出目录/基名 (模型 后缀).扩展名
                outputBase = os.path.join(outputDir, f'{base} ({self.comboModel.currentText()} {suffix})')
                self.progressValue[2] += 1
                itemId = self.addFileItem(inputPath)
                if ext.lower() == '.gif':
                    queue.append(task.SplitGIFTask(self.sigOutput.emit, self.progressValue, inputPath, outputBase + '.gif', initialConfigParams, queue, self.checkOptimizeGIF.isChecked(), itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                elif self.entryCustomCommand.text().strip():
                    t = tempfile.mktemp('.png')
                    queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, inputPath, t, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                    queue.append(task.CustomCompressTask(self.sigOutput.emit, t, outputBase + '.png', self.entryCustomCommand.text().strip(), True, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                elif self.checkLossyMode.isChecked():
                    t = tempfile.mktemp('.webp')
                    outputPath = outputBase + ('.webp' if self.checkUseWebP.isChecked() else '.jpg')
                    queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, inputPath, t, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                    queue.append(task.LossyCompressTask(self.sigOutput.emit, t, outputPath, self.spinLossyQuality.value(), True, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                else:
                    outputPath = outputBase + ('.webp' if self.checkUseWebP.isChecked() else '.png')
                    queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, inputPath, outputPath, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))

            self.processing = True
            self.processingPaused = False
            self.pauseEvent.set()
            self.cancelEvent.clear()
            self.updateProcessButton()
            self.textOutput.clear()

            if sys.platform != 'darwin':
                self.notification = notifypy.Notify(
                    default_notification_application_name=define.APP_TITLE,
                    default_notification_icon=os.path.join(define.BASE_PATH, 'asset', 'icons', 'icon-128px.png'),
                )
            ts = time.perf_counter()
            def completeCallback(withError: bool):
                self.sigComplete.emit(withError)
            def failCallback(ex: Exception, itemId: int | None = None):
                self.sigFail.emit(f'{type(ex).__name__}: {ex}')
                if itemId is not None:
                    self.sigItemState.emit(itemId, 'failed')
            self.notificationOutputPath = outputDir
            self.notificationTimeStart = ts

            self.logFile = open(self.logPath, 'w', encoding='utf-8')
            t = threading.Thread(
                target=task.taskRunner,
                args=(
                    queue,
                    self.pauseEvent,
                    self.cancelEvent,
                    self.sigOutput.emit,
                    completeCallback,
                    failCallback,
                    self.sigFinally.emit,
                    self.checkIgnoreError.isChecked(),
                    self.taskStartCallback,
                )
            )
            t.start()
        except Exception as ex:
            QMessageBox.critical(self, define.APP_TITLE, traceback.format_exc())

    def onTaskComplete(self, withError: bool):
        te = time.perf_counter()
        if sys.platform != 'darwin':
            self.notification.title = '处理完成'
            if withError:
                self.notification.message = '……但是出现了错误。\n请检查输出或日志文件：{0}'.format(self.logPath)
            else:
                self.notification.message = '输出的文件已保存到：{0}\n耗时：{1:.03f}s'.format(self.notificationOutputPath, te - self.notificationTimeStart)
            self.notification.send(False)

    def onTaskFail(self, message: str):
        if sys.platform != 'darwin':
            self.notification.title = '处理失败'
            self.notification.message = message
            self.notification.send(False)

    def onTaskFinally(self):
        # 用户主动停止时：处理中的行标记为“已停止”，等待中的行标记为“未处理”
        wasCancelled = self.cancelEvent.is_set()
        self.processing = False
        self.processingPaused = False
        self.pauseEvent.set()
        self.cancelEvent.clear()
        self.updateProcessButton()
        if wasCancelled:
            for i in range(self.treeFiles.topLevelItemCount()):
                item = self.treeFiles.topLevelItem(i)
                state = item.data(1, Qt.ItemDataRole.UserRole)
                if state == 'processing':
                    self.setItemState(item, 'stopped')
                elif state == 'waiting':
                    self.setItemState(item, 'skipped')
        self.logFile.close()

    def setInputPath(self, paths: tuple[str, ...]):
        self.entryInputPath.setText(' | '.join(paths))
        # 输出目录默认填第一个输入文件所在目录，用户可再改
        self.entryOutputPath.setText(os.path.dirname(os.path.abspath(paths[0])) if paths else '')

    def writeToOutput(self, s: str):
        if self.logFile:
            self.logFile.write(s)
        # 子进程的百分比进度行不进 GUI 文本框（只写日志文件），进度改由进度条显示
        if not re.fullmatch(r'[\d.,\s%]+', s):
            self.textOutput.moveCursor(QTextCursor.MoveOperation.End)
            self.textOutput.insertPlainText(s)
            vsb = self.textOutput.verticalScrollBar()
            if vsb.maximum() == 0 or vsb.pageStep() > vsb.maximum() * .5 or vsb.value() > vsb.maximum() * .9:
                vsb.setValue(vsb.maximum())

    def addFileItem(self, path: str) -> int:
        """在文件列表中新增一行，返回行号（即任务的 itemId）。"""
        itemId = self.treeFiles.topLevelItemCount()
        item = QTreeWidgetItem((os.path.basename(path), '', ''))
        self.treeFiles.addTopLevelItem(item)
        bar = QProgressBar(self.treeFiles, minimum=0, maximum=100)
        bar.setValue(0)
        self.treeFiles.setItemWidget(item, 2, bar)
        self.setItemState(item, 'waiting')
        return itemId

    def setItemState(self, item: QTreeWidgetItem, state: str):
        item.setData(1, Qt.ItemDataRole.UserRole, state)
        item.setText(1, ITEM_STATE_LABELS[state])
        bar = self.treeFiles.itemWidget(item, 2)
        if bar is not None:
            # 失败/停止的行内进度条通过 QSS 属性着色
            bar.setProperty('state', state)
            bar.style().unpolish(bar)
            bar.style().polish(bar)

    def onItemState(self, itemId: int, state: str):
        item = self.treeFiles.topLevelItem(itemId)
        if item is None:
            return
        self.setItemState(item, state)

    def onItemProgress(self, itemId: int, fraction: float):
        item = self.treeFiles.topLevelItem(itemId)
        if item is None:
            return
        bar = self.treeFiles.itemWidget(item, 2)
        if bar is not None:
            bar.setValue(round(fraction * 100))
        if fraction >= 1:
            self.setItemState(item, 'done')

    def taskProgressCallback(self, itemId: int | None, fraction: float):
        if itemId is not None:
            self.sigItemProgress.emit(itemId, fraction)

    def taskStartCallback(self, t: 'task.AbstractTask'):
        if t.itemId is not None:
            self.sigItemState.emit(t.itemId, 'processing')

    def getConfigParams(self) -> param.REConfigParams:
        resizeMode = param.ResizeMode(self.resizeModeGroup.checkedId())
        resizeModeValue = 0
        match resizeMode:
            case param.ResizeMode.RATIO:
                resizeModeValue = self.spinResizeRatio.value()
            case param.ResizeMode.WIDTH:
                resizeModeValue = self.spinResizeWidth.value()
            case param.ResizeMode.HEIGHT:
                resizeModeValue = self.spinResizeHeight.value()
            case param.ResizeMode.LONGEST_SIDE:
                resizeModeValue = self.spinResizeLongestSide.value()
            case param.ResizeMode.SHORTEST_SIDE:
                resizeModeValue = self.spinResizeShortestSide.value()
        return param.REConfigParams(
            self.comboModel.currentText(),
            self.modelFactors[self.comboModel.currentText()],
            self.config['Config'].get('ModelDir') or os.path.join(define.APP_PATH, 'models'),
            resizeMode,
            resizeModeValue,
            self.downsample[self.comboDownsample.currentIndex()][1],
            self.tileSize[self.comboTileSize.currentIndex()],
            self.spinGPUID.value(),
            self.checkUseTTA.isChecked(),
            self.checkPreupscale.isChecked(),
            self.entryCustomCommand.text().strip(),
        )

# 配置与模型路径在主窗口创建前初始化：
# 启动时若缺少主程序或模型需要弹出警告，因此必须先完成配置与模型扫描
def init_config_and_model_paths() -> tuple[configparser.ConfigParser, list[str]]:
    config = configparser.ConfigParser({
        'Upscaler': '',
        'ModelDir': '',
        'ResizeMode': int(param.ResizeMode.RATIO),
        'ResizeRatio': 4,
        'ResizeWidth': 1024,
        'ResizeHeight': 1024,
        'ResizeLongestSide': 1024,
        'ResizeShortestSide': 1024,
        'Model': '',
        'DownsampleIndex': 0,
        'GPUID': -1,
        'TileSizeIndex': 0,
        'LossyQuality': 80,
        'UseWebP': False,
        'UseTTA': False,
        'OptimizeGIF': False,
        'LossyMode': False,
        'IgnoreError': False,
        'Preupscale': False,
        'CustomCommand': '',
    })
    config['Config'] = {}
    config.read(define.APP_CONFIG_PATH)

    if config['Config'].get('Upscaler'):
        define.RE_PATH = os.path.realpath(config['Config'].get('Upscaler'))

    try:
        modelDir = config['Config'].get('ModelDir') or os.path.join(define.APP_PATH, 'models')
        if os.path.splitext(os.path.split(define.RE_PATH)[1])[0] == 'realcugan-ncnn-vulkan':
            # 兼容Real-CUGAN的模型文件名格式
            # https://github.com/nihui/realcugan-ncnn-vulkan/blob/395302c5c70f1bff604c974e92e0a87e45c9f9ee/src/main.cpp#L733
            # -m model-path
            # -s scale
            # -n noise-level
            # <model-path>/up<scale>x-conservative.{param,bin}
            # <model-path>/up<scale>x-no-denoise.{param,bin}
            # <model-path>/up<scale>x-denoise<noise-level>x.{param,bin}
            models = []
            for name, scale, noise in itertools.product(
                sorted(x for x in os.listdir(modelDir) if os.path.isdir(os.path.join(modelDir, x))),
                range(2, 5),
                ('conservative', 'no-denoise', *(f'denoise{i}x' for i in range(1, 4))),
            ):
                if all(os.path.exists(os.path.join(modelDir, name, f'up{scale}x-{noise}.{ext}')) for ext in ('bin', 'param')):
                    models.append(f'{name}#up{scale}x-{noise}')
        else:
            modelFiles = set(x for x in os.listdir(modelDir) if os.path.isfile(os.path.join(modelDir, x)))
            models = sorted(
                x for x in set(os.path.splitext(y)[0] for y in modelFiles)
                if f'{x}.bin' in modelFiles and f'{x}.param' in modelFiles
            )
    except FileNotFoundError:
        # in case of FileNotFoundError exception, return empty modelFiles and models.
        # This does not change any behabiour because in this case
        # we will be showing a warning message and terminate app
        models = []

    return config, models

if __name__ == '__main__':
    os.chdir(define.APP_PATH)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(define.APP_TITLE)

    config, models = init_config_and_model_paths()

    if not os.path.exists(define.RE_PATH) or not models:
        QMessageBox.warning(None, define.APP_TITLE, '未找到 Real-ESRGAN-ncnn-vulkan 主程序。\n请前往 https://github.com/xinntao/Real-ESRGAN/releases 下载，并将本文件和主程序放在同一目录下。')
        webbrowser.open_new_tab('https://github.com/xinntao/Real-ESRGAN/releases')
        sys.exit(0)

    app = REGUIApp(config, models)
    app.setWindowTitle(define.APP_TITLE)
    app.setWindowIcon(QIcon(os.path.join(define.BASE_PATH, 'asset', 'icons', 'icon-256px.ico')))

    # 主题不再跟随系统：读取配置，缺失或非法值默认浅色
    theme = config['Config'].get('Theme', fallback='Light')
    app.applyTheme('Dark' if theme == 'Dark' else 'Light')

    initialSize = (720, 640)
    app.setMinimumSize(*initialSize)
    app.resize(*initialSize)
    screenGeometry = qapp.primaryScreen().availableGeometry()
    app.move(
        screenGeometry.x() + (screenGeometry.width() - initialSize[0]) // 2,
        screenGeometry.y() + (screenGeometry.height() - initialSize[1]) // 2,
    )

    # 最好用的一个 要是第一次通过拖放打开文件路径就好了 · Issue #45 · TransparentLC/realesrgan-gui
    # https://github.com/TransparentLC/realesrgan-gui/issues/45
    if len(sys.argv) > 1:
        app.setInputPath(sys.argv[1:])

    app.show()
    sys.exit(qapp.exec())
