# 贡献这个项目

[English version](#Contributing)

感谢你对 Real-ESRGAN GUI 这个项目的贡献～

如果你认为 Real-ESRGAN GUI 帮到了你，除了做出贡献以外，也可以通过以下方式表达对我的支持：

* 为这个项目点 ⭐Star
* 在你的个人网站或微信公众号等平台介绍 Real-ESRGAN GUI
  * 但是，请不要使用“关注可见”、“回复可见”甚至是“付费可见”这样的方式
* 在你发布使用 Real-ESRGAN GUI 处理的图片时，添加“使用了 Real-ESRGAN GUI 进行放大”这样的介绍，并且附上这个项目的链接

## 报告 Bug，请求新功能，或是其他的问题

请先查找[已有的 Issue](https://github.com/TransparentLC/realesrgan-gui/issues?q=is%3Aissue)，看看是否有人已经提出了类似的问题。如果仍然有疑问的话，你可以[提交新的 Issue](https://github.com/TransparentLC/realesrgan-gui/issues/new)。

如果你遇到了 bug，请提供你认为与 bug 有关的重要信息，例如运行环境、日志输出、复现过程、会触发的 bug 的图片等。

> [!TIP]
> 请确认你遇到的 bug 确实与这个 GUI 相关，而不是来自 Real-ESRGAN 本身。
>
> 例如，在 Real-ESRGAN 处理失败时你可能会在 GUI 看到 `subprocess.CalledProcessError: Command ... returned non-zero exit status ...` 这样的输出，你可以在命令行中自行执行相关命令来验证这是否为 Real-ESRGAN 本身的问题。

如果你希望添加新功能，请在动手之前先在 Issue 中进行讨论。你可以介绍这一功能的细节，可以起到的作用等。

> [!TIP]
> 与其他将大量超分辨率技术整合到一起的 GUI 相比，Real-ESRGAN GUI 的目标是尽可能做到实用又不失简洁和轻量，因此目前不会考虑加入对其他超分辨率技术的支持。

你也可以自行修复 bug 或实现新功能并提交 Pull Requests，在 merge 之前我可能会提出 review。

# Contributing

Thanks for taking the time to contribute to this repository!

If you like Real-ESRGAN GUI, you can also show your appreciation in the following ways, which I would also be happy about:

* ⭐Star this repository
* Recommend Real-ESRGAN GUI on your website, blog, social media, etc.
  * However, please don't put this repository's link behind any sort of paywall
* Add "Upscaled with Real-ESRGAN GUI" and this repository's link to the description if you are publishing images upscaled with Real-ESRGAN GUI

## Bug report, feature requests, or other questions

Search for [existing issues](https://github.com/TransparentLC/realesrgan-gui/issues?q=is%3Aissue) that might help you at first. If you still feel the need to ask a question and need clarification, you can open a [new issue](https://github.com/TransparentLC/realesrgan-gui/issues/new).

When submitting bug reports, please provide any important information you think is relevant to the bug. For example, the environment, log outputs, detailed steps to reproduce the bug, and the image that triggers the bug.

> [!TIP]
> Make sure that your bug is really related to this GUI and not from Real-ESRGAN itself.
>
> You might see `subprocess.CalledProcessError: Command ... returned non-zero exit status ...` from the GUI's output when Real-ESRGAN failed to process the image. You can determine if this bug is from Real-ESRGAN itself by executing the commands in the command line.

Before working on new features, please open an issue to discuss it at first. You can provide details of your feature request, describe the behavior you are expected to see and explain why this feature would be beneficial to you and other users.

> [!TIP]
> In contrast to other all-in-one GUIs which integrate a large number of upscalers (or super-resolution tools), Real-ESRGAN GUI aims to be as practical as possible while still being simple and lightweight. Therefore I have no plans to support other upscalers.

You can fix the bug or implement the new feature by yourself and submit a pull request. I might start reviews before merging it.