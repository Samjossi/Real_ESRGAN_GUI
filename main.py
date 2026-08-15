import collections
import configparser
import itertools
import locale
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
from PySide6.QtCore import QTimer
from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtGui import QIcon
from PySide6.QtGui import QPixmap
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QButtonGroup
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
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
from PySide6.QtWidgets import QTreeWidget
from PySide6.QtWidgets import QTreeWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

import define
import i18n
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

# 文件列表行状态 -> i18n 文案键
ITEM_STATE_LABEL_KEYS = {
    'waiting': 'ItemStateWaiting',
    'processing': 'ItemStateProcessing',
    'done': 'ItemStateDone',
    'failed': 'ItemStateFailed',
    'stopped': 'ItemStateStopped',
    'skipped': 'ItemStateSkipped',
}

class REGUIApp(QMainWindow):
    # 工作线程通过信号把日志/回调投递到 GUI 线程，Qt 不允许跨线程直接操作界面
    sigOutput = Signal(str)
    sigComplete = Signal(bool)
    sigFail = Signal(str)
    sigFinally = Signal()
    sigTheme = Signal(str)
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

        self.outputPathChanged = True
        self.logPath = os.path.join(define.APP_PATH, 'output.log')
        self.logFile: typing.IO = None
        # 当前的放大进度（0~1）/已放大的文件/总共要放大的文件
        self.progressValue: list[int | float] = [0, 0, 1]
        # 初始值/结束值/进度（进度条动画，三次方缓动）
        self.progressAnimation: list[float] = [0, 0, 0]
        self.progressCurrent = 0.0
        self.progressAnimTimer = QTimer(self, interval=10)
        self.progressAnimTimer.timeout.connect(self.progressAnimStep)
        # 处理状态
        self.processing = False
        self.processingPaused = False
        # 当前正在处理的文件列表行号（驱动“当前文件”进度条）
        self.currentItemId: int | None = None
        # 任务栏进度条
        if sys.platform == 'win32':
            import comtypes.client
            comtypes.client.GetModule(os.path.join(define.BASE_PATH, 'TaskbarLib.tlb'))
            import comtypes.gen.TaskbarLib
            self.progressNativeTaskbar = comtypes.client.CreateObject('{56FDF344-FD6D-11d0-958A-006097C9A090}', interface=comtypes.gen.TaskbarLib.ITaskbarList3)
            self.progressNativeTaskbar.HrInit()
            self.progressNativeTaskbar.ActivateTab(int(self.winId()))
            self.progressNativeTaskbar.SetProgressState(int(self.winId()), 0) # TBPF_NOPROGRESS
        else:
            self.progressNativeTaskbar = None
        # 控制是否暂停
        self.pauseEvent = threading.Event()
        # 控制是否停止（取消）处理
        self.cancelEvent = threading.Event()

        self.sigOutput.connect(self.writeToOutput, Qt.ConnectionType.QueuedConnection)
        self.sigComplete.connect(self.onTaskComplete, Qt.ConnectionType.QueuedConnection)
        self.sigFail.connect(self.onTaskFail, Qt.ConnectionType.QueuedConnection)
        self.sigFinally.connect(self.onTaskFinally, Qt.ConnectionType.QueuedConnection)
        self.sigTheme.connect(self.applyTheme, Qt.ConnectionType.QueuedConnection)
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

        self.labelInputPath = QLabel(self.frameBasicConfig)
        basicLayout.addWidget(self.labelInputPath)
        inputRow = QHBoxLayout()
        self.entryInputPath = QLineEdit(self.frameBasicConfig)
        inputRow.addWidget(self.entryInputPath, 1)
        self.buttonInputPath = QPushButton(self.frameBasicConfig)
        self.buttonInputPath.clicked.connect(self.buttonInputPath_click)
        inputRow.addWidget(self.buttonInputPath)
        basicLayout.addLayout(inputRow)

        self.labelOutputPath = QLabel(self.frameBasicConfig)
        basicLayout.addWidget(self.labelOutputPath)
        outputRow = QHBoxLayout()
        self.entryOutputPath = QLineEdit(self.frameBasicConfig)
        # 手动修改输出路径后，缩放参数变化时不再自动重算
        self.entryOutputPath.textEdited.connect(lambda: setattr(self, 'outputPathChanged', True))
        outputRow.addWidget(self.entryOutputPath, 1)
        self.buttonOutputPath = QPushButton(self.frameBasicConfig)
        self.buttonOutputPath.clicked.connect(self.buttonOutputPath_click)
        outputRow.addWidget(self.buttonOutputPath)
        basicLayout.addLayout(outputRow)

        bottomRow = QHBoxLayout()
        frameResize = QWidget(self.frameBasicConfig)
        resizeLayout = QGridLayout(frameResize)
        resizeLayout.setContentsMargins(0, 0, 0, 0)
        self.labelResizeMode = QLabel(frameResize)
        resizeLayout.addWidget(self.labelResizeMode, 0, 0, 1, 2)
        self.resizeModeGroup = QButtonGroup(self)
        self.radioResizeRatio = QRadioButton(frameResize)
        self.spinResizeRatio = QSpinBox(frameResize, minimum=2, maximum=16)
        resizeLayout.addWidget(self.radioResizeRatio, 1, 0)
        resizeLayout.addWidget(self.spinResizeRatio, 1, 1)
        self.radioResizeWidth = QRadioButton(frameResize)
        self.spinResizeWidth = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeWidth, 2, 0)
        resizeLayout.addWidget(self.spinResizeWidth, 2, 1)
        self.radioResizeHeight = QRadioButton(frameResize)
        self.spinResizeHeight = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeHeight, 3, 0)
        resizeLayout.addWidget(self.spinResizeHeight, 3, 1)
        self.radioResizeLongestSide = QRadioButton(frameResize)
        self.spinResizeLongestSide = QSpinBox(frameResize, minimum=1, maximum=16383)
        resizeLayout.addWidget(self.radioResizeLongestSide, 4, 0)
        resizeLayout.addWidget(self.spinResizeLongestSide, 4, 1)
        self.radioResizeShortestSide = QRadioButton(frameResize)
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
        self.labelUsedModel = QLabel(self.frameBasicConfig)
        rightColumn.addWidget(self.labelUsedModel)
        self.comboModel = QComboBox(self.frameBasicConfig)
        self.comboModel.addItems(self.models)
        rightColumn.addWidget(self.comboModel)
        rightColumn.addStretch(1)
        self.buttonStop = QPushButton(self.frameBasicConfig)
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
        self.labelDownsampleMode = QLabel(self.frameAdvancedConfig)
        downColumn.addWidget(self.labelDownsampleMode)
        self.comboDownsample = QComboBox(self.frameAdvancedConfig)
        self.comboDownsample.addItems(tuple(x[0] for x in self.downsample))
        downColumn.addWidget(self.comboDownsample)
        downTileRow.addLayout(downColumn, 1)
        tileColumn = QVBoxLayout()
        self.labelTileSize = QLabel(self.frameAdvancedConfig)
        tileColumn.addWidget(self.labelTileSize)
        self.comboTileSize = QComboBox(self.frameAdvancedConfig)
        tileColumn.addWidget(self.comboTileSize)
        downTileRow.addLayout(tileColumn, 1)
        leftColumn.addLayout(downTileRow)

        self.labelUsedGPUID = QLabel(self.frameAdvancedConfig)
        leftColumn.addWidget(self.labelUsedGPUID)
        self.spinGPUID = QSpinBox(self.frameAdvancedConfig, minimum=-1, maximum=7)
        leftColumn.addWidget(self.spinGPUID)
        self.labelLossyModeQuality = QLabel(self.frameAdvancedConfig)
        leftColumn.addWidget(self.labelLossyModeQuality)
        self.spinLossyQuality = QSpinBox(self.frameAdvancedConfig, minimum=0, maximum=100, singleStep=5)
        leftColumn.addWidget(self.spinLossyQuality)
        self.labelCustomCommand = QLabel(self.frameAdvancedConfig)
        leftColumn.addWidget(self.labelCustomCommand)
        self.entryCustomCommand = QLineEdit(self.frameAdvancedConfig)
        leftColumn.addWidget(self.entryCustomCommand)
        leftColumn.addStretch(1)
        advancedLayout.addLayout(leftColumn, 1)

        rightColumnAdv = QVBoxLayout()
        self.checkUseWebP = QCheckBox(self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkUseWebP)
        self.checkUseTTA = QCheckBox(self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkUseTTA)
        self.checkOptimizeGIF = QCheckBox(self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkOptimizeGIF)
        self.checkLossyMode = QCheckBox(self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkLossyMode)
        self.checkIgnoreError = QCheckBox(self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkIgnoreError)
        self.checkPreupscale = QCheckBox(self.frameAdvancedConfig)
        rightColumnAdv.addWidget(self.checkPreupscale)
        self.comboLanguage = QComboBox(self.frameAdvancedConfig)
        self.comboLanguage.addItems(tuple(i18n.locales_map.keys()))
        rightColumnAdv.addWidget(self.comboLanguage)
        rightColumnAdv.addStretch(1)
        advancedLayout.addLayout(rightColumnAdv, 3)

        # ---- 关于 ----
        self.frameAbout = QWidget(self)
        aboutLayout = QVBoxLayout(self.frameAbout)
        aboutLayout.addStretch(1)
        labelIcon = QLabel(self.frameAbout)
        labelIcon.setPixmap(QPixmap(os.path.join(define.BASE_PATH, 'icon-128px.png')))
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
        self.buttonViewREGUISource = QPushButton(self.frameAbout)
        self.buttonViewREGUISource.clicked.connect(lambda: webbrowser.open_new_tab('https://github.com/TransparentLC/realesrgan-gui'))
        aboutButtonGrid.addWidget(self.buttonViewREGUISource, 0, 0)
        self.buttonViewRESource = QPushButton(self.frameAbout)
        self.buttonViewRESource.clicked.connect(lambda: webbrowser.open_new_tab('https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan'))
        aboutButtonGrid.addWidget(self.buttonViewRESource, 0, 1)
        self.buttonViewAdditionalModel = QPushButton(self.frameAbout)
        self.buttonViewAdditionalModel.clicked.connect(lambda: webbrowser.open_new_tab('https://github.com/TransparentLC/realesrgan-gui/releases/tag/additional-models'))
        aboutButtonGrid.addWidget(self.buttonViewAdditionalModel, 1, 0)
        self.buttonViewDonatePage = QPushButton(self.frameAbout)
        self.buttonViewDonatePage.clicked.connect(lambda: webbrowser.open_new_tab('https://i.akarin.dev/donate/'))
        aboutButtonGrid.addWidget(self.buttonViewDonatePage, 1, 1)
        aboutButtonRow = QHBoxLayout()
        aboutButtonRow.addStretch(1)
        aboutButtonRow.addLayout(aboutButtonGrid)
        aboutButtonRow.addStretch(1)
        aboutLayout.addLayout(aboutButtonRow)
        aboutLayout.addStretch(1)

        self.notebookConfig.addTab(self.frameBasicConfig, '')
        self.notebookConfig.addTab(self.frameAdvancedConfig, '')
        self.notebookConfig.addTab(self.frameAbout, '')

        # ---- 文件列表（逐项显示处理状态与进度） ----
        self.treeFiles = QTreeWidget(self)
        self.treeFiles.setColumnCount(3)
        self.treeFiles.setRootIsDecorated(False)
        self.treeFiles.setUniformRowHeights(True)
        self.treeFiles.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.treeFiles.setColumnWidth(1, 80)
        self.treeFiles.setColumnWidth(2, 160)
        centralLayout.addWidget(self.treeFiles, 1)

        # ---- 日志输出与进度条 ----
        self.textOutput = QPlainTextEdit(self)
        self.textOutput.setReadOnly(True)
        centralLayout.addWidget(self.textOutput, 1)

        # 当前正在处理的这一张图的进度（0~1 对应 0~1000）
        self.progressbarCurrentFile = QProgressBar(self, minimum=0, maximum=1000)
        self.progressbarCurrentFile.setValue(0)
        centralLayout.addWidget(self.progressbarCurrentFile)

        self.progressbar = QProgressBar(self, minimum=0, maximum=1000)
        self.progressbar.setValue(0)
        centralLayout.addWidget(self.progressbar)

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
        self.entryCustomCommand.setText(c.get('CustomCommand'))
        self.comboLanguage.setCurrentIndex(i18n.get_current_locale_display_name())

        self.retranslateUi()
        self.comboTileSize.setCurrentIndex(c.getint('TileSizeIndex'))

        # ---- 信号连接（初始值恢复完成后再连，避免误触发输出路径重算）----
        self.resizeModeGroup.idToggled.connect(self.outputPathTraceCallback)
        self.spinResizeRatio.valueChanged.connect(self.outputPathTraceCallback)
        self.spinResizeWidth.valueChanged.connect(self.outputPathTraceCallback)
        self.spinResizeHeight.valueChanged.connect(self.outputPathTraceCallback)
        self.spinResizeLongestSide.valueChanged.connect(self.outputPathTraceCallback)
        self.spinResizeShortestSide.valueChanged.connect(self.outputPathTraceCallback)
        self.comboModel.currentTextChanged.connect(self.outputPathTraceCallback)
        self.comboLanguage.currentIndexChanged.connect(self.change_app_lang)

        # 子控件默认会接受文本拖拽，关闭后拖拽事件才会冒泡到主窗口统一处理
        for w in (self.entryInputPath, self.entryOutputPath, self.entryCustomCommand, self.textOutput):
            w.setAcceptDrops(False)
        self.setAcceptDrops(True)

    def retranslateUi(self):
        self.notebookConfig.setTabText(0, i18n.getTranslatedString('FrameBasicConfig'))
        self.notebookConfig.setTabText(1, i18n.getTranslatedString('FrameAdvancedConfig'))
        self.notebookConfig.setTabText(2, i18n.getTranslatedString('FrameAbout'))

        self.labelInputPath.setText(i18n.getTranslatedString('Input'))
        self.labelOutputPath.setText(i18n.getTranslatedString('Output'))
        self.buttonInputPath.setText(i18n.getTranslatedString('OpenFileDialog'))
        self.buttonOutputPath.setText(i18n.getTranslatedString('OpenFileDialog'))
        self.labelUsedModel.setText(i18n.getTranslatedString('UsedModel'))
        self.labelResizeMode.setText(i18n.getTranslatedString('ResizeMode'))
        self.radioResizeRatio.setText(i18n.getTranslatedString('ResizeModeRatio'))
        self.radioResizeWidth.setText(i18n.getTranslatedString('ResizeModeWidth'))
        self.radioResizeHeight.setText(i18n.getTranslatedString('ResizeModeHeight'))
        self.radioResizeLongestSide.setText(i18n.getTranslatedString('ResizeModeLongestSide'))
        self.radioResizeShortestSide.setText(i18n.getTranslatedString('ResizeModeShortestSide'))
        self.buttonStop.setText(i18n.getTranslatedString('StopProcessing'))
        self.treeFiles.setHeaderLabels((
            i18n.getTranslatedString('ItemColumnFile'),
            i18n.getTranslatedString('ItemColumnState'),
            i18n.getTranslatedString('ItemColumnProgress'),
        ))
        self.updateProcessButton()
        self.labelDownsampleMode.setText(i18n.getTranslatedString('DownsampleMode'))

        self.labelTileSize.setText(i18n.getTranslatedString('TileSize'))
        # Tile 尺寸下拉首项是翻译文本，需要随语言切换重译
        tileSizeIndex = self.comboTileSize.currentIndex()
        self.comboTileSize.blockSignals(True)
        self.comboTileSize.clear()
        self.comboTileSize.addItem(i18n.getTranslatedString('TileSizeAuto'))
        self.comboTileSize.addItems(tuple(str(x) for x in self.tileSize[1:]))
        self.comboTileSize.setCurrentIndex(max(tileSizeIndex, 0))
        self.comboTileSize.blockSignals(False)

        self.labelUsedGPUID.setText(i18n.getTranslatedString('UsedGPUID'))
        self.labelLossyModeQuality.setText(i18n.getTranslatedString('LossyModeQuality'))
        self.labelCustomCommand.setText(i18n.getTranslatedString('CustomCommand'))
        self.checkUseWebP.setText(i18n.getTranslatedString('PreferWebP'))
        self.checkUseTTA.setText(i18n.getTranslatedString('EnableTTA'))
        self.checkOptimizeGIF.setText(i18n.getTranslatedString('GIFOptimizeTransparency'))
        self.checkLossyMode.setText(i18n.getTranslatedString('EnableLossyMode'))
        self.checkIgnoreError.setText(i18n.getTranslatedString('EnableIgnoreError'))
        self.checkPreupscale.setText(i18n.getTranslatedString('EnablePreupscale'))
        self.buttonViewREGUISource.setText(i18n.getTranslatedString('ViewREGUISource'))
        self.buttonViewRESource.setText(i18n.getTranslatedString('ViewRESource'))
        self.buttonViewAdditionalModel.setText(i18n.getTranslatedString('ViewAdditionalModel'))
        self.buttonViewDonatePage.setText(i18n.getTranslatedString('ViewDonatePage'))

    def updateProcessButton(self):
        self.buttonProcess.setText(i18n.getTranslatedString(('ContinueProcessing' if self.processingPaused else 'PauseProcessing') if self.processing else 'StartProcessing'))
        self.setButtonAccent(self.buttonProcess, not (self.processing and not self.processingPaused))
        self.buttonStop.setEnabled(self.processing and not self.cancelEvent.is_set())

    def buttonStop_click(self):
        if not self.processing or self.cancelEvent.is_set():
            return
        self.cancelEvent.set()
        # 暂停状态点停止 = 立即停止：先放行，让工作线程走到取消检查
        self.pauseEvent.set()
        self.processingPaused = False
        self.writeToOutput(i18n.getTranslatedString('StoppingProcessing') + '\n')
        self.updateProcessButton()

    @staticmethod
    def setButtonAccent(button: QPushButton, accent: bool):
        button.setProperty('accent', 'true' if accent else 'false')
        button.style().unpolish(button)
        button.style().polish(button)

    def change_app_lang(self, index: int):
        i18n.set_current_language(i18n.locales_map[self.comboLanguage.currentText()])
        self.retranslateUi()

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
            'CustomCommand': self.entryCustomCommand.text(),
            'AppLanguage': i18n.current_language
        }
        with open(define.APP_CONFIG_PATH, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = tuple(u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile() and u.toLocalFile())
        if paths:
            self.setInputPath(paths)
        event.acceptProposedAction()

    def buttonInputPath_click(self):
        p, _ = QFileDialog.getOpenFileNames(
            self,
            filter='Image files (*.jpg *.jpeg *.png *.gif *.webp *.tif *.tiff)',
        )
        if not p:
            return
        self.setInputPath(tuple(p))

    def buttonOutputPath_click(self):
        p, _ = QFileDialog.getSaveFileName(
            self,
            filter='Image files (*.png *.gif *.webp)',
        )
        if not p:
            return
        self.entryOutputPath.setText(p)

    def outputPathTraceCallback(self, *args):
        if not self.outputPathChanged:
            self.setInputPath(tuple(p.strip() for p in self.entryInputPath.text().split('|')))

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
            inputPaths = tuple(p.strip() for p in self.entryInputPath.text().split('|'))
            outputPaths = tuple(p.strip() for p in self.entryOutputPath.text().split('|'))
            if not inputPaths or not outputPaths or len(inputPaths) != len(outputPaths):
                return QMessageBox.warning(self, define.APP_TITLE, i18n.getTranslatedString('WarningInvalidPath'))

            initialConfigParams = self.getConfigParams()
            if initialConfigParams.resizeMode == param.ResizeMode.RATIO and initialConfigParams.resizeModeValue == 1:
                return QMessageBox.warning(self, define.APP_TITLE, i18n.getTranslatedString('WarningResizeRatio'))

            self.progressValue[0] = 0
            self.progressValue[1] = 0
            self.progressValue[2] = 0
            queue = collections.deque()
            # 重建文件列表：每个输入文件一行，行号即任务的 itemId
            self.treeFiles.clear()
            self.currentItemId = None
            self.progressbarCurrentFile.setValue(0)
            for inputPath, outputPath in zip(inputPaths, outputPaths):
                inputPath = os.path.normpath(inputPath)
                outputPath = os.path.normpath(outputPath)
                if not os.path.exists(inputPath):
                    return QMessageBox.warning(self, define.APP_TITLE, i18n.getTranslatedString('WarningNotFoundPath'))

                if os.path.isdir(inputPath):
                    for curDir, dirs, files in os.walk(inputPath):
                        for f in files:
                            if os.path.splitext(f)[1].lower() not in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.tif', '.tiff'}:
                                continue
                            f = os.path.join(curDir, f)
                            g = os.path.join(outputPath, f.removeprefix(inputPath + os.path.sep))
                            itemId = self.addFileItem(f)
                            if os.path.splitext(f)[1].lower() == '.gif':
                                queue.append(task.SplitGIFTask(self.sigOutput.emit, self.progressValue, f, g, initialConfigParams, queue, self.checkOptimizeGIF.isChecked(), itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                            elif self.entryCustomCommand.text().strip():
                                t = tempfile.mktemp('.png')
                                g = os.path.splitext(g)[0] + ('.webp' if self.checkUseWebP.isChecked() else '.png')
                                queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, f, t, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                                queue.append(task.CustomCompressTask(self.sigOutput.emit, t, g, self.entryCustomCommand.text().strip(), True, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                            elif self.checkLossyMode.isChecked():
                                t = tempfile.mktemp('.webp')
                                g = os.path.splitext(g)[0] + ('.webp' if self.checkUseWebP.isChecked() else '.jpg')
                                queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, f, t, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                                queue.append(task.LossyCompressTask(self.sigOutput.emit, t, g, self.spinLossyQuality.value(), True, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                            else:
                                g = os.path.splitext(g)[0] + ('.webp' if self.checkUseWebP.isChecked() else '.png')
                                queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, f, g, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                            self.progressValue[2] += 1
                    if not queue:
                        return QMessageBox.warning(self, define.APP_TITLE, i18n.getTranslatedString('WarningEmptyFolder'))
                elif os.path.splitext(inputPath)[1].lower() in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.tif', '.tiff'}:
                    self.progressValue[2] += 1
                    itemId = self.addFileItem(inputPath)
                    if os.path.splitext(inputPath)[1].lower() == '.gif':
                        queue.append(task.SplitGIFTask(self.sigOutput.emit, self.progressValue, inputPath, outputPath, initialConfigParams, queue, self.checkOptimizeGIF.isChecked(), itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                    elif self.entryCustomCommand.text().strip():
                        t = tempfile.mktemp('.png')
                        queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, inputPath, t, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                        queue.append(task.CustomCompressTask(self.sigOutput.emit, t, outputPath, self.entryCustomCommand.text().strip(), True, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                    elif self.checkLossyMode.isChecked() and os.path.splitext(outputPath)[1].lower() in {'.jpg', '.jpeg', '.webp'}:
                        t = tempfile.mktemp('.webp')
                        queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, inputPath, t, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                        queue.append(task.LossyCompressTask(self.sigOutput.emit, t, outputPath, self.spinLossyQuality.value(), True, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                    else:
                        queue.append(task.RESpawnTask(self.sigOutput.emit, self.progressValue, inputPath, outputPath, initialConfigParams, itemId=itemId, progressCallback=self.taskProgressCallback, cancelEvent=self.cancelEvent))
                else:
                    return QMessageBox.warning(self, define.APP_TITLE, i18n.getTranslatedString('WarningInvalidFormat'))

            self.setProgress(0)
            self.progressAnimation[0] = 0
            self.progressAnimation[1] = 0
            self.progressAnimation[2] = 0
            self.progressAnimTimer.stop()

            self.processing = True
            self.processingPaused = False
            self.pauseEvent.set()
            self.cancelEvent.clear()
            self.updateProcessButton()
            self.textOutput.clear()

            if sys.platform != 'darwin':
                self.notification = notifypy.Notify(
                    default_notification_application_name=define.APP_TITLE,
                    default_notification_icon=os.path.join(define.BASE_PATH, 'icon-128px.png'),
                )
            if sys.platform == 'win32':
                self.progressNativeTaskbar.SetProgressState(int(self.winId()), 2) # TBPF_NORMAL
                # 初始进度应该是0，但是直接设为0没有效果，所以改成使用非常接近0的值
                self.progressNativeTaskbar.SetProgressValue(int(self.winId()), 1, 0xFFFFFFFF)
            ts = time.perf_counter()
            def completeCallback(withError: bool):
                self.sigComplete.emit(withError)
            def failCallback(ex: Exception, itemId: int | None = None):
                self.sigFail.emit(f'{type(ex).__name__}: {ex}')
                if itemId is not None:
                    self.sigItemState.emit(itemId, 'failed')
            self.notificationOutputPath = outputPath
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
            self.notification.title = i18n.getTranslatedString('ToastCompletedTitle')
            if withError:
                self.notification.message = i18n.getTranslatedString('ToastCompletedMessageWithError').format(self.logPath)
            else:
                self.notification.message = i18n.getTranslatedString('ToastCompletedMessage').format(self.notificationOutputPath, te - self.notificationTimeStart)
            self.notification.send(False)
        self.progressAnimTimer.stop()
        self.setProgress(100)

    def onTaskFail(self, message: str):
        if sys.platform != 'darwin':
            self.notification.title = i18n.getTranslatedString('ToastFailedTitle')
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
        if sys.platform == 'win32':
            self.progressNativeTaskbar.SetProgressState(int(self.winId()), 0) # TBPF_NOPROGRESS

    def setInputPath(self, paths: tuple[str, ...]):
        self.entryInputPath.setText(' | '.join(paths))
        self.entryOutputPath.setText(self.getOutputPath(paths))
        self.outputPathChanged = False

    def setProgress(self, value: float):
        self.progressCurrent = value
        self.progressbar.setValue(round(value * 10))

    def progressAnimStep(self):
        self.setProgress(self.progressAnimation[0] + (self.progressAnimation[1] - self.progressAnimation[0]) * (lambda x: 1 - (1 - x) ** 3)(self.progressAnimation[2]))
        self.progressAnimation[2] += 1 / 10
        if self.progressAnimation[2] >= 1:
            self.progressAnimTimer.stop()

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
        self.updateTotalProgress()

    def updateTotalProgress(self):
        progressFrom = self.progressCurrent
        progressTo = (self.progressValue[0] + self.progressValue[1]) / self.progressValue[2] * 100
        if progressFrom != progressTo:
            self.progressAnimTimer.stop()
            self.progressAnimation[0] = progressFrom
            self.progressAnimation[1] = progressTo
            self.progressAnimation[2] = 0
            self.progressAnimTimer.start()
            if sys.platform == 'win32':
                self.progressNativeTaskbar.SetProgressState(int(self.winId()), 2) # TBPF_NORMAL
                self.progressNativeTaskbar.SetProgressValue(int(self.winId()), round(progressTo), 100)

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
        item.setText(1, i18n.getTranslatedString(ITEM_STATE_LABEL_KEYS[state]))
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
        if state == 'processing' and self.currentItemId != itemId:
            self.currentItemId = itemId
            self.progressbarCurrentFile.setValue(0)

    def onItemProgress(self, itemId: int, fraction: float):
        item = self.treeFiles.topLevelItem(itemId)
        if item is None:
            return
        bar = self.treeFiles.itemWidget(item, 2)
        if bar is not None:
            bar.setValue(round(fraction * 100))
        # 当前文件进度条与列表行进度同源
        if itemId == self.currentItemId:
            self.progressbarCurrentFile.setValue(round(fraction * 1000))
        self.updateTotalProgress()
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

    def getOutputPath(self, paths: tuple[str, ...]) -> str:
        r = []
        for p in paths:
            if os.path.isdir(p):
                base, ext = p, ''
            else:
                base, ext = os.path.splitext(p)
                if ext.lower() in {'.jpg', '.tif', '.tiff'} or self.entryCustomCommand.text().strip():
                    ext = '.png'
                if ext.lower() == '.png' and self.checkUseWebP.isChecked():
                    ext = '.webp'
            suffix = ''
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
            r.append(f'{base} ({self.comboModel.currentText()} {suffix}){ext}')
        return ' | '.join(r)

# Config and model paths are initialized before main frame
# Because for the WarningNotFoundRE warning message app language
# must be initialized and for that config must be initialized
# and for that models variable needs to be set
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
        'AppLanguage': locale.getdefaultlocale()[0],
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

    i18n.set_current_language(config['Config'].get('AppLanguage'))
    return config, models

if __name__ == '__main__':
    os.chdir(define.APP_PATH)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(define.APP_TITLE)

    config, models = init_config_and_model_paths()

    if not os.path.exists(define.RE_PATH) or not models:
        QMessageBox.warning(None, define.APP_TITLE, i18n.getTranslatedString('WarningNotFoundRE'))
        webbrowser.open_new_tab('https://github.com/xinntao/Real-ESRGAN/releases')
        sys.exit(0)

    app = REGUIApp(config, models)
    app.setWindowTitle(define.APP_TITLE)
    app.setWindowIcon(QIcon(os.path.join(define.BASE_PATH, 'icon-256px.ico')))

    try:
        import darkdetect
        app.applyTheme(darkdetect.theme())
        if sys.platform in {'win32', 'linux'}:
            t = threading.Thread(target=darkdetect.listener, args=(app.sigTheme.emit,))
            t.daemon = True
            t.start()
    except Exception:
        print(traceback.format_exc())
        app.applyTheme('Light')

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
