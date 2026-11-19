from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

from PIL import Image, ImageFont
import re
import os
import configparser
import json
import datetime
import time
import shutil
import emoji
import traceback

from src import emojiRender, fontResolver
from src.errcode import errcode


class DrawingQuery:
    """PDF查询相关功能，包括字宽，图片大小等

    """
    def __init__(self, ERRCODE: errcode.ErrCode, paths, style, log):
        """

        :param ERRCODE: errcode.ErrCode
        :param paths: 相关路径字典
        :param style: 样式设置字典
        """
        self.ERRCODE = ERRCODE
        self.paths = paths
        self.style = style
        self.log = log
        self._emoji_warned = False

        # 字宽直接由字体渲染度量计算：与旧 _aspect_ratio.db 的 getsize 结果一致，
        # 只是按字符惰性计算并缓存，省掉 37 MB 的预生成数据库。
        fontPath = paths.get("fontPath")
        fontIndex = paths.get("fontIndex", 0)
        self._font = (
            ImageFont.truetype(fontPath, 16, index=fontIndex) if fontPath else None
        )
        self._ratio_cache: dict[str, float] = {}
        fallback = self._bbox_ratio("\U0010FFFE") if self._font else None
        self._fallback_ratio = fallback if fallback is not None else 1.0

    def _bbox_ratio(self, char):
        """返回字符渲染 bbox 的宽高比，无法渲染时返回 None。"""
        if self._font is None:
            return None
        try:
            bbox = self._font.getbbox(char)
        except Exception:
            return None
        if not bbox or bbox[3] == 0:
            return None
        return bbox[2] / bbox[3]

    def query_char_width(self, char):
        """查询字体宽度

        :param char: 字符
        :return: 宽度
        """
        aspect_ratio = self._ratio_cache.get(char)
        if aspect_ratio is None:
            aspect_ratio = self._bbox_ratio(char)
            if aspect_ratio is None:
                aspect_ratio = self._fallback_ratio
            self._ratio_cache[char] = aspect_ratio
        return round(aspect_ratio * self.style["textHeight"])

    def resize_image(self, img_width, img_height, max_width, max_height):
        """计算图片缩放后的大小

        :param img_width: 图片宽度
        :param img_height: 图片高度
        :param max_width: 最大宽度
        :param max_height: 最大高度
        :return: 新宽度，新高度
        """
        # 比较宽高的大小，计算缩放比例
        ratio = min(max_width / img_width, max_height / img_height)
        # 根据比例计算新的宽高
        new_width = int(img_width * ratio)
        new_height = int(img_height * ratio)
        return new_width, new_height

    def convert_filesize(self, size):
        """将以字节为单位的文件大小转换成合适的单位，并保留两位小数。

        :param size: 文件大小，单位为字节。
        :return: 字符串格式的文件大小，包含单位。
        """
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        index = 0
        while size >= 1024 and index < 4:
            size /= 1024
            index += 1
        return '{:.2f}{}'.format(size, units[index])

    def get_emoji_info(self, str):
        """获取文本中的emoji信息

        :param str: 输入字符串
        :return:

        示例输出：[{'match_start': 2, 'match_end': 5, 'emoji': '👩‍🚀'}, {'match_start': 15, 'match_end': 16, 'emoji': '🚀'}]
        """
        return emoji.emoji_list(str)

    def is_emoji_start(self, emoji_info, pos):
        """输入emoji_info和当前位置，返回当前位置是否是emoji字符的起始位置。
        若是起始位置，则一并返回emoji字符。

        :param emoji_info: 文本中的emoji信息
        :param pos: 当前位置
        :return: 是否在emoji中，若是起始位置则返回emoji
        """
        for e in emoji_info:
            if e['match_start'] == pos:
                return True, e['emoji']
            elif e['match_start'] < pos < e['match_end']:
                return True, None
        return False, None

    def get_unicode_file(self, emoji_unicode):
        """将 emoji 光栅化为 PNG（复用系统彩色 emoji 字体）

        :param emoji_unicode: emoji字符
        :return: PNG 路径，渲染失败时为 None
        """
        filePath = emojiRender.emoji_png(emoji_unicode)
        if filePath is None and not self._emoji_warned:
            self._emoji_warned = True
            if emojiRender.font_candidates():
                self.log("⚠️ 系统 emoji 字体缺少部分表情，已跳过无法渲染的 emoji")
            else:
                self.log("⚠️ 未找到系统彩色 emoji 字体，emoji 将被跳过")
        return filePath


class PdfDraw:
    """
    直接操作PDF数据的层，除绘制书签外，一般不直接调用，通过PDF消息处理层调用
    基本上后三个参数为x，y，c，xy为左下角基点绘制坐标，c为column列数
    """
    def __init__(self, ERRCODE: errcode.ErrCode, drawingQuery: DrawingQuery, paths, style, log):
        """

        :param ERRCODE: errcode.ErrCode
        :param drawingQuery: DrawingQuery
        :param paths: 相关路径字典
        :param style: 样式设置字典
        """

        self.ERRCODE = ERRCODE
        self.drawingQuery = drawingQuery
        self.paths = paths
        self.style = style
        self.log = log

        if self.paths.get("fontPath"):
            pdfmetrics.registerFont(
                TTFont(
                    self.style["fontName"],
                    self.paths["fontPath"],
                    subfontIndex=self.paths.get("fontIndex", 0),
                )
            )
        else:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            self.style["fontName"] = "STSong-Light"
        self.pdf_canvas = canvas.Canvas("output/chatData.pdf", pagesize=self.style["pageSize"])
        self.drawPageFooter(1)

    def save(self):
        """保存PDF文件

        """
        self.pdf_canvas.save()

    # pdf绘制页脚
    def drawPageFooter(self, pageNum):
        """绘制指定页码的页面底部文本

        :param pageNum: 当前页码
        """
        text = self.style["pageFooterText"].replace("$page", str(pageNum))
        self.pdf_canvas.setFillColor(self.style["textColor"])
        self.pdf_canvas.setFont(self.style["fontName"], self.style["pageFooterTextHeight"])
        self.pdf_canvas.drawString(self.style["leftMargin"], self.style["bottomMargin"], text)

    # pdf翻页
    def nextPage(self, pageNum):
        """结束当前页面，并开始绘制下一页。同时绘制下一页的页面底部文本
        若需翻页请调用Generate.nextPage而不是此函数

        :param pageNum: 当前页码
        """
        self.pdf_canvas.showPage()
        self.drawPageFooter(pageNum)

    def drawText(self, text, x, y, c):
        """绘制文本

        :param text: 要绘制的文本
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        self.pdf_canvas.setFillColor(self.style["textColor"])
        self.pdf_canvas.setFont(self.style["fontName"], self.style["textHeight"])
        self.pdf_canvas.drawString(x, y, text)

    def drawName(self, text, x, y, c):
        """绘制名称

        :param text: 要绘制的名称文本
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        self.pdf_canvas.setFillColor(self.style["nameTextColor"])
        self.pdf_canvas.setFont(self.style["fontName"], self.style["nameTextHeight"])
        self.pdf_canvas.drawString(x, y, text)

    def drawTipText(self, text, x, y, c):
        """绘制灰条提示文本

        :param text: 要绘制的提示文本
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        self.pdf_canvas.setFillColor(self.style["tipTextColor"])
        self.pdf_canvas.setFont(self.style["fontName"], self.style["tipTextHeight"])
        x = self.style["pageWidth"] * c + self.style["contentCenter"]
        y = y
        self.pdf_canvas.drawCentredString(x, y, text)

    def drawTextQQEmoji(self, path, x, y, c):
        """绘制QQ表情

        :param path: QQ表情图像的路径
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """

        x = self.style["pageWidth"] * c + x
        path = self.paths["outputDirPath"] + path
        self.pdf_canvas.drawImage(path, x, y,
                                  width=self.style["qqemojiWidth"], height=self.style["qqemojiWidth"],
                                  mask='auto')

    def drawImg(self, path, name, width, height, x, y, c):
        """绘制图像并绘制图像名称文本

        :param path: 图像文件的路径
        :param name: 图像的名称
        :param width: 图像的宽度
        :param height: 图像的高度
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        # print("Img", x, y, width, height)

        path = self.paths["outputDirPath"] + path
        if name:
            self.pdf_canvas.drawImage(path, x, y + self.style["textHeight"],
                                      width=width, height=height,
                                      mask='auto')

            text = f"file:  {name}"
            self.pdf_canvas.setFillColor(self.style["textColor"])
            self.pdf_canvas.setFont(self.style["fontName"], self.style["textHeight"])
            self.pdf_canvas.drawString(x, y, text)
        else:
            self.pdf_canvas.drawImage(path, x, y,
                                      width=width, height=height,
                                      mask='auto')

    def drawTextEmoji(self, char, x, y, c):
        """绘制文本unicode表情

        :param char: 要绘制的emoji字符
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 页绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x - 10

        path = self.drawingQuery.get_unicode_file(char)
        if path is None:
            return
        self.pdf_canvas.drawImage(path, x, y,
                                  width=self.style["emojiWidth"], height=self.style["emojiWidth"],
                                  mask='auto')


    def drawChatBox(self, width, hight, x, y, c):
        """绘制聊天框

        :param width: 聊天框的宽度
        :param hight: 聊天框的高度
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 页绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        self.pdf_canvas.setFillColor(self.style["chatBoxFillColor"])
        self.pdf_canvas.roundRect(x, y, width, hight, self.style["chatBoxRadius"],
                                  fill=1, stroke=0)

    def drawReplyBox(self, width, hight, x, y, c):
        """绘制回复框

        :param width: 回复框的宽度
        :param hight: 回复框的高度
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        self.pdf_canvas.setFillColor(self.style["replyBoxFillColor"])
        self.pdf_canvas.roundRect(x, y, width, hight, self.style["chatBoxRadius"],
                                  fill=1, stroke=0)

    def drawGeneralBox(self, width, hight, x, y, c):
        """绘制通用框

        :param width: 通用框的宽度
        :param hight: 通用框的高度
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        self.pdf_canvas.setStrokeColor(self.style["generalBoxFillColor"])
        self.pdf_canvas.roundRect(x, y, width, hight, self.style["chatBoxRadius"],
                                  fill=0, stroke=1)

    def drawAvatar(self, path, size, x, y, c):
        """绘制头像

        :param path: 头像图像的路径
        :param size: 头像的尺寸
        :param x: 绘制起始点的 x 坐标
        :param y: 绘制起始点的 y 坐标
        :param c: 绘制起始点的 c 坐标（列）
        """
        x = self.style["pageWidth"] * c + x
        # print("Img", x, y, width, height)

        path = self.paths["outputDirPath"] + path

        if not os.path.exists(path):
            if self.style["emptyAvatarShow"] == "true":
                return
            self.log(f"头像文件{path}不存在")
            return
        self.pdf_canvas.drawImage(path, x, y,
                                  width=size, height=size,
                                  mask='auto')

    def bookmark(self, str, y, level):
        """添加书签

        :param str: 书签的名称
        :param y: 书签在页面上的垂直位置
        :param level: 书签的级别
        """
        self.pdf_canvas.addOutlineEntry(str, str, level)
        self.pdf_canvas.bookmarkHorizontalAbsolute(str, y, left=0, fit='XYZ')


class DataProcessor:
    """PDF消息处理层

    接收垂直空间，处理消息数据，返回绘制细节数据drawData
    DataProcessor的函数不直接绘制内容，而是返回drawData
    drawData说明：[函数,[函数参数],[x,y,c]]
    """
    def __init__(self, ERRCODE: errcode.ErrCode, paths, style, senders, pdfDraw: PdfDraw, drawingQuery: DrawingQuery, log):
        """PDF消息处理层
        接收垂直空间，处理消息数据，返回绘制细节数据

        :param ERRCODE: errcode.ErrCode
        :param path: 相关路径字典
        :param style: 样式设置字典
        :param senders: 发送者信息字典
        :param pdfDraw: PdfDraw
        :param drawingQuery: DrawingQuery
        """

        self.drawingQuery = drawingQuery
        self.pdfDraw = pdfDraw
        self.ERRCODE = ERRCODE
        self.paths = paths
        self.style = style
        self.senders = senders

    def procAvatar(self, senderUin, heightSpace):
        """处理头像绘制相关的细节，并返回绘制细节数据

        :param senderUin: 发送者Uin
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息高度，绘制数据
        """
        MsgHeight = self.style["avatarSize"] + self.style["avatarMargin"]
        MsgWidth = self.style["avatarSize"] + 2 * self.style["avatarMargin"]
        if MsgHeight > heightSpace:
            return False, MsgHeight, None
        if senderUin in  self.senders:
            senderAvatarPath = self.senders[senderUin][1]
            detaY = - self.style["avatarSize"]
            drawData = [[self.pdfDraw.drawAvatar, [senderAvatarPath, self.style["avatarSize"]],
                         [-MsgWidth, detaY, 0]]]

            return True, MsgHeight, drawData
        else:

            return True, MsgHeight, []


    def procName(self, senderUin, heightSpace):
        """处理姓名绘制相关的细节，并返回绘制细节数据

        :param senderUin: 发送者Uin
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息高度，绘制数据
        """
        MsgHeight = self.style["nameTextHeight"] + self.style["nameSpacing"]
        if MsgHeight > heightSpace:
            return False, MsgHeight, None

        if senderUin in  self.senders:
            senderName = self.senders[senderUin][0]
        else: senderName = "未知"
        detaY = - self.style["nameTextHeight"]
        drawData = [[self.pdfDraw.drawName, [senderName], [0, detaY, 0]]]

        return True, MsgHeight, drawData

    # def procErrMessage(self, data, heightSpace):
    #
    #     isFinish = False
    #     isFinish, textHeight, textWidth, drawData, remaindData \
    #         = self.processMessageList(drawData, heightSpace)
    #
    #     errBoxHeight = textHeight + 2 * self.style["chatBoxPadding"]
    #     errBoxWidth = textWidth + 2 * self.style["chatBoxPadding"]
    #     errBoxY = - errBoxHeight
    #
    #     if isFinish:
    #         # 绘制错误框
    #         for item in drawData:
    #             # 将内容偏移
    #             item[2][0] += self.style["chatBoxPadding"]
    #         drawData.insert(0, [self.pdfDraw.drawMsgBox, [errBoxWidth, errBoxHeight], [0, errBoxY, 0]])
    #         return isFinish, errBoxHeight, drawData
    #     return isFinish, errBoxHeight, None

    def procDetailMessage(self, data, heightSpace):
        """根据给定的数据和可用的垂直空间绘制信息，并返回绘制细节数据
        支持file,uns,err,call,nudge

        :param data: 消息数据
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息框高度，绘制数据
        """
        dataType = data["t"]
        if dataType == "file":
            drawText = f'发送文件：{data["c"]["name"]}'
            if "fileSize" in data["c"]:
                fileSize = data["c"]["size"]
                fileSizeStr = self.drawingQuery.convert_filesize(fileSize)
                drawText += f"\n文件大小：{fileSizeStr}"
        elif dataType == "uns" or dataType == "err":
            drawText = f'{data["c"]["text"]}'
        elif dataType == "call":
            drawText = f'{data["c"]["text"]}'
        elif dataType == "nudge":
            drawText = f'{data["c"]["text"]}'
        elif dataType == "voice":
            drawText = f'语音：{data["c"]["length"]}秒，路径{data["c"]["path"]}'
        drawData = [{
            "t": "m",
            "c": {"m": drawText}
        }]
        isFinish = False
        isFinish, textHeight, textWidth, drawData, remaindData \
            = self.processMessageList(drawData, heightSpace - self.style["chatBoxPadding"])

        generalBoxHeight = textHeight + 2 * self.style["chatBoxPadding"]
        generalBoxWidth = textWidth + 2 * self.style["chatBoxPadding"]
        generalBoxY = - generalBoxHeight

        if isFinish:
            # 绘制框
            for item in drawData:
                # 将内容偏移
                item[2][0] += self.style["chatBoxPadding"]
            drawData.insert(0, [self.pdfDraw.drawGeneralBox, [generalBoxWidth, generalBoxHeight], [0, generalBoxY, 0]])
            return isFinish, generalBoxHeight, drawData
        return isFinish, generalBoxHeight, None

    def procTimeMessage(self, data, heightSpace):
        """根据给定的数据和可用的垂直空间处理时间信息，并返回绘制细节数据

        :param data: 消息数据
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息框高度，绘制数据
        """
        text = data
        MsgHeight = self.style["tipTextHeight"]
        if MsgHeight > heightSpace:
            return False, MsgHeight, None
        drawData = [[self.pdfDraw.drawTipText, [text], [0, - self.style["tipTextHeight"], 0]]]
        return True, MsgHeight, drawData

    def procTipMessage(self, data, heightSpace):
        """根据给定的数据和可用的垂直空间处理灰条提示信息，并返回绘制细节数据

        :param data: 消息数据
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息框高度，绘制数据
        """
        text = data["c"]["text"]
        MsgHeight = self.style["tipTextHeight"]
        if MsgHeight > heightSpace:
            return False, MsgHeight, None
        drawData = [[self.pdfDraw.drawTipText, [text], [0, - self.style["tipTextHeight"], 0]]]
        return True, MsgHeight, drawData


    def procImgVideoMessage(self, data, heightSpace):
        """根据给定的数据和可用的垂直空间处理图片视频，并返回绘制细节数据

        :param data: 消息数据
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息框高度，绘制数据
        """

        width, height, name, path = self.getImgData(data)

        if name:
            msgHeight = self.style["tipTextHeight"] + height
            detaY = - height - self.style["textHeight"]

        else:
            msgHeight = height
            detaY = - height
        drawData = [[self.pdfDraw.drawImg, [path, name, width, height], [0, detaY, 0]]]

        if msgHeight > heightSpace:
            return False, msgHeight, None
        return True, msgHeight, drawData

    def procChatBoxMessage(self, dataList, heightSpace):
        """根据给定的数据和可用的垂直空间处理带框消息（普通消息，混合消息，回复消息等），并返回绘制细节数据

        :param dataList: 消息数据
        :param heightSpace: 可用的垂直空间
        :return: 是否绘制完毕，消息框高度，绘制数据，在当前页是否已经开始绘制，剩余数据
        """
        isFinish = False
        isStart = False
        isFinish, textHeight, textWidth, drawData, remaindData \
            = self.processMessageList(dataList, heightSpace - self.style["chatBoxPadding"])

        chatBoxHeight = textHeight + 2 * self.style["chatBoxPadding"]
        chatBoxWidth = textWidth + 2 * self.style["chatBoxPadding"]
        chatBoxY = - chatBoxHeight

        # 绘制聊天框
        if len(drawData) != 0:
            isStart = True

            for item in drawData:
                # 将内容偏移到聊天框内
                item[2][0] += self.style["chatBoxPadding"]
                item[2][1] -= self.style["chatBoxPadding"]

            remaindData.insert(0, {
                "t": "m",
                "c": {"m": "【续上：】"}
            })
            drawData.insert(0, [self.pdfDraw.drawChatBox, [chatBoxWidth, chatBoxHeight], [0, chatBoxY, 0]])
        return isFinish, chatBoxHeight, drawData, isStart, remaindData

    def getImgData(self, data):
        """获取图片大小等数据
        注意，一般不直接调用

        :param data: 消息数据
        :return: width,height
        """
        if data["t"] == "video":
            path = data["c"]["thumb"]
            name = data["c"]["name"]
            imgType = "video"
        elif data["t"] == "img":
            path = data["c"]["path"]
            name = data["c"]["name"]
            imgType = data["c"]["type"]

        # 使用PIL读取图片的真实长宽
        # print(imgType)
        with Image.open(self.paths["outputDirPath"] + path) as img:
            imgWidth, imgHeight = img.size
        # 如果是图片表情
        if imgType == "marketface":
            maxWidth = self.style["imgEmoMaxSize"]
            maxHeight = self.style["imgEmoMaxSize"]
            name = ""
        elif imgType == "pic":
            if data["c"]["size"] < self.style["intEmoPicSepSize"]*1000:
                maxWidth = self.style["imgEmoMaxSize"]
                maxHeight = self.style["imgEmoMaxSize"]
                name = ""
            else:
                maxWidth = self.style["imgMaxWidth"]
                maxHeight = self.style["imgMaxHeight"]
        else:
            maxWidth = self.style["imgMaxWidth"]
            maxHeight = self.style["imgMaxHeight"]

        width, height = self.drawingQuery.resize_image(imgWidth, imgHeight, maxWidth, maxHeight)
        return width, height, name, path



    def processMessageList(self, dataList, heightSpace):
        """根据给定的数据和可用的垂直空间处理混合文本列表，并返回绘制细节数据
        本函数包含计算文本换行，手动文本排版等内容
        注意，一般不直接调用

        :param dataList: 消息数据
        :param heightSpace: 可用的垂直空间
        :return: 文本高度，文本宽度，绘制数据，剩余数据
        """
        def lineBreak():
            """根据当前绘制状态和样式进行换行操作，并更新绘制数据、当前坐标等

            :return: 是否成功换行
            """
            nonlocal buffer, bufStartX, curX, curY, heightSpace, textHeight, textWidth
            if buffer != "":
                # 绘制缓冲区字符
                drawData.append([self.pdfDraw.drawText, [buffer], [bufStartX, curY - heightSpace, 0]])
                buffer = ""

            # 更新当前坐标到下一行开头，并清空暂存字符串
            if curX > textWidth:
                textWidth = curX
            curX = 0
            bufStartX = curX

            textHeight += self.style["textHeight"] + self.style["lineSpacing"]

            if curY - self.style["textHeight"] - self.style["lineSpacing"] < 0:
                return False
            else:
                curY -= self.style["textHeight"] + self.style["lineSpacing"]
                return True

        buffer = ""
        drawData = []
        textWidth = 0
        textHeight = 0
        curX = 0
        curY = heightSpace
        bufStartX = 0
        lastItem = "start"


        # 遍历列表中的每一个元素
        for itemNum in range(len(dataList)):
            item = dataList[itemNum]
            # 处理 "m" 类型的元素
            if item["t"] == "m":
                if lastItem in ["start", "img", "reply"]:
                    # 校正初次换行
                    textHeight -= self.style["textHeight"] + self.style["lineSpacing"]
                    if not lineBreak():
                        # 没有空间用来换行
                        return False, textHeight, textWidth, drawData, dataList[itemNum:]
                    else:
                        curY += self.style["lineSpacing"]

                bufStartX = curX
                lastItem = "m"
                # 暂存一行中的文本
                buffer = ""
                # 遍历字符串中的每一个字符
                msgStr = item["c"]["m"]
                emojiInfo = self.drawingQuery.get_emoji_info(item["c"]["m"])
                for charNum in range(len(msgStr)):
                    character = msgStr[charNum]
                    # 判断字符是否为表情符号
                    isInEmoji, emojiStr = self.drawingQuery.is_emoji_start(emojiInfo, charNum)
                    if isInEmoji:
                        if emojiStr:
                            # print(emojiInfo, msgStr)
                            # print(emojiStr, character)
                            # 如果是emoji初位置，则绘制符号，并更新当前坐标
                            if buffer != "":
                                drawData.append([self.pdfDraw.drawText, [buffer], [bufStartX, curY - heightSpace, 0]])
                                buffer = ""


                            if curX + self.style["emojiWidth"] > self.style["chatBoxTextMaxWidth"]:
                                # 如果该字符加上前面已暂存字符串的宽度会超出列宽，则将暂存字符串绘制出来并换行
                                if not lineBreak():
                                    remaindData = []
                                    item["c"]["m"] = msgStr[charNum:]
                                    remaindData.append(item)
                                    if dataList[itemNum + 1:] != []:
                                        remaindData.extend(dataList[itemNum + 1:])
                                    return False, textHeight, textWidth, drawData, remaindData
                            curX += self.style["emojiWidth"]
                            bufStartX = curX
                            drawData.append([self.pdfDraw.drawTextEmoji, [emojiStr], [curX, curY - heightSpace, 0]])
                            if curX > textWidth:
                                textWidth = curX


                        else:
                            continue

                    elif character == "\n":
                        if not lineBreak():
                            remaindData = []
                            item["c"]["m"] = msgStr[charNum:]
                            remaindData.append(item)
                            if dataList[itemNum + 1:] != []:
                                remaindData.extend(dataList[itemNum + 1:])
                            return False, textHeight, textWidth, drawData, remaindData

                    else:
                        # 如果不是表情符号，先查询其宽度
                        charWidth = self.drawingQuery.query_char_width(character)
                        # 判断是否需要换行
                        if curX + charWidth > self.style["chatBoxTextMaxWidth"]:
                            # 如果该字符加上前面已暂存字符串的宽度会超出列宽，则将暂存字符串绘制出来并换行
                            if not lineBreak():
                                remaindData = []
                                item["c"]["m"] = msgStr[charNum:]
                                remaindData.append(item)
                                if dataList[itemNum + 1:] != []:
                                    remaindData.extend(dataList[itemNum + 1:])
                                return False, textHeight, textWidth, drawData, remaindData

                        # 将当前字符加入缓冲区中
                        buffer += character
                        curX += charWidth
                # 处理剩余的暂存字符串
                if buffer:
                    if curX > textWidth:
                        textWidth = curX

                    drawData.append([self.pdfDraw.drawText, [buffer], [bufStartX, curY - heightSpace, 0]])
                    bufStartX = curX


            # 处理 qqemoji 类型的元素
            elif item["t"] == "qqemoji":
                if lastItem in ["start", "img", "reply"]:
                    # 校正初次换行
                    textHeight -= self.style["textHeight"] + self.style["lineSpacing"]
                    if not lineBreak():
                        # 没有空间用来换行
                        return False, textHeight, textWidth, drawData, dataList[itemNum:]
                    else:
                        curY += self.style["lineSpacing"]

                lastItem = "qqemoji"
                if curX + self.style["qqemojiWidth"] > self.style["chatBoxTextMaxWidth"]:
                    # 如果该字符加上前面已暂存字符串的宽度会超出列宽，则先将暂存字符串绘制出来
                    if not lineBreak():
                        remaindData = []
                        remaindData.extend(dataList[itemNum:])
                        return False, textHeight, textWidth, drawData, remaindData

                # 绘制qq表情符号并更新坐标
                drawData.append([self.pdfDraw.drawTextQQEmoji, [item["c"]["path"]], [curX, curY - heightSpace, 0]])
                curX += self.style["qqemojiWidth"]
                if curX > textWidth:
                    textWidth = curX



            elif item["t"] == "reply":
                lastItem = "reply"
                sourceMsgSenderUin = str(item["c"]["uin"])
                if sourceMsgSenderUin in self.senders:
                    senderInfo = self.senders[sourceMsgSenderUin][0]
                else:
                    senderInfo = sourceMsgSenderUin
                sourceMsgTime = item["c"]["time"]
                sourceMsgTimeStr = time.strftime("%m月%d日 %H:%M", time.localtime(sourceMsgTime))
                sourceMsgText = item["c"]["text"]
                sourceMsgInfo = f"\n{senderInfo} {sourceMsgTimeStr}\n{sourceMsgText}"

                replyMsgHeight = 0
                replyMsgWidth = 0
                replyMsgCurY = curY
                # 暂存一行中的文本
                buffer = ""
                buffers = []
                curX = 0
                # 遍历字符串中的每一个字符
                for charNum in range(len(sourceMsgInfo)):
                    character = sourceMsgInfo[charNum]

                    if character == "\n":
                        buffers.append([buffer, replyMsgCurY])
                        buffer = ""
                        replyMsgHeight += self.style["textHeight"] + self.style["lineSpacing"]
                        replyMsgCurY -= self.style["textHeight"] + self.style["lineSpacing"]
                        curX = 0

                    else:
                        charWidth = self.drawingQuery.query_char_width(character)
                        # 判断是否需要换行
                        if curX + charWidth > self.style["chatBoxTextMaxWidth"] - 2 * self.style["chatBoxPadding"]:
                            # 如果该字符加上前面已暂存字符串的宽度会超出列宽，则将暂存字符串绘制出来并换行
                            buffers.append([buffer, replyMsgCurY])
                            buffer = ""
                            replyMsgHeight += self.style["textHeight"] + self.style["lineSpacing"]
                            replyMsgCurY -= self.style["textHeight"] + self.style["lineSpacing"]

                            curX = 0

                        # 将当前字符加入缓冲区中
                        buffer += character
                        curX += charWidth
                        if curX > replyMsgWidth:
                            replyMsgWidth = curX

                # 处理剩余的暂存字符串
                if buffer:
                    if curX > replyMsgWidth:
                        replyMsgWidth = curX
                    buffers.append([buffer, replyMsgCurY])
                    buffer = ""
                    replyMsgHeight += self.style["chatBoxPadding"]
                    replyMsgCurY -= self.style["chatBoxPadding"]
                    curX = 0

                if replyMsgCurY < 0:
                    remaindData = []
                    remaindData.extend(dataList[itemNum:])
                    return False, textHeight, textWidth, drawData, remaindData

                if replyMsgWidth > textWidth:
                    textWidth = replyMsgWidth
                textHeight += replyMsgHeight
                curY -= replyMsgHeight
                drawData.append([self.pdfDraw.drawReplyBox, [replyMsgWidth, textHeight], [0, curY- heightSpace, 0]])
                for bufferItem, drawY in buffers:
                    drawData.append([self.pdfDraw.drawText, [bufferItem], [self.style["chatBoxPadding"], drawY - heightSpace, 0]])


            # 处理 "img" 类型的元素
            elif item["t"] == "img":
                lastItem = "img"
                width, height, name, path = self.getImgData(item)

                if name:
                    msgHeight = self.style["tipTextHeight"] + height

                else:
                    msgHeight = height

                # print(msgHeight, "msgHeight")


                if curY - msgHeight < 0:
                    remaindData = []
                    remaindData.extend(dataList[itemNum:])
                    return False, textHeight, textWidth, drawData, remaindData

                # 绘制图片并更新坐标
                drawData.append([self.pdfDraw.drawImg,
                                 [path, name, width, height], [0, curY - heightSpace - msgHeight, 0]])
                textHeight += msgHeight
                curY -= msgHeight
                curX = 0
                if width > textWidth:
                    textWidth = width

        # 留出最后一行的位置
        textHeight += self.style["textHeight"] + self.style["lineSpacing"]
        return True, textHeight, textWidth, drawData, []


class Generate:
    """生成PDF总控制层

    """
    def __init__(self, ERRCODE: errcode.ErrCode, path, style,
                 pdfDraw: PdfDraw, dataprocessor: DataProcessor, log):
        """

        :param ERRCODE: errcode.ErrCode
        :param path: 相关路径字典
        :param style: 样式设置字典
        :param pdfDraw: PdfDraw
        :param dataprocessor: DataProcessor
        """
        self.pdfDraw = pdfDraw
        self.dataprocessor = dataprocessor
        self.ERRCODE = ERRCODE
        self.paths = path
        self.style = style
        self.log = log
        self.pageNum = 1
        self.curY = self.style["contentStartY"]
        self.curC = 0
        self.lastTime = 0
        self.lastDate = 0
        self.lastMonth = 0
        self.lastYear = 0


    def nextPage(self):
        """根据当前的页码和列数，判断是否需要翻页或是换列，并更新相关的页码和列数。

        """
        if self.curC + 1 < self.style["intcolumn"]:
            self.curC = self.curC + 1
        else:
            self.pageNum += 1
            self.curC = 0
            self.pdfDraw.nextPage(self.pageNum)
        self.curY = self.style["contentStartY"]

    def procTime(self, thisTime):
        """时间管理，是否显示时间，添加书签等，在绘制每条消息前调用

        :param thisTime: 当前消息时间戳
        :return:
        """
        thisDate = time.strftime("%Y年%m月%d日", time.localtime(thisTime))
        thisMonth = time.strftime("%Y年%m月", time.localtime(thisTime))
        thisYear = time.strftime("%Y年", time.localtime(thisTime))
        timeStr = time.strftime("%Y年%m月%d日 %H:%M", time.localtime(thisTime))

        if thisYear != self.lastYear:
            self.drawMsg(self.dataprocessor.procTimeMessage, thisYear, False, False)
            self.pdfDraw.bookmark(thisYear, self.curY + self.style["tipTextHeight"], 0)
        if thisMonth != self.lastMonth:
            self.drawMsg(self.dataprocessor.procTimeMessage, thisMonth, False, False)
            self.pdfDraw.bookmark(thisMonth, self.curY + self.style["tipTextHeight"], 1)
        if thisDate != self.lastDate:
            self.drawMsg(self.dataprocessor.procTimeMessage, thisDate, False, False)
            self.pdfDraw.bookmark(thisDate, self.curY + self.style["tipTextHeight"], 2)

        # 相差n分钟,添加时间标签
        diffMin = abs(self.lastTime - thisTime) / 60
        if diffMin >= self.style["intDiffMin"]:
            self.drawMsg(self.dataprocessor.procTimeMessage, timeStr, False, False)

            self.curY -= self.style["MassageSpacing"]
            if self.curY <= self.style["contentMaxY"]:
                self.nextPage()

        self.lastTime = thisTime
        self.lastDate = thisDate
        self.lastMonth = thisMonth
        self.lastYear = thisYear

    def drawDataRun(self, drawData, StartX, StartY, StartC):
        """运行绘制函数

        :param drawData: 格式为[[func,[args],[x,y,c(column)]]]的绘制命令列表
        :param StartX: X偏移量
        :param StartY: Y偏移量
        :param StartC: Column偏移量
        :return:无
        """
        for item in drawData:
            item[2][0] = item[2][0] + StartX
            item[2][1] = item[2][1] + StartY
            item[2][2] = item[2][2] + StartC
            # 将item[1]的所有项作为参数给函数item[0]
            item[0](*item[1], *item[2])

    def drawMsg(self, drawFunc, msgData, isDivisible, isWithAvatar):
        """绘制消息

        :param drawFunc: 绘制消息函数
        :param msgData: 消息内容
        :param isDivisible: 消息是否可分割
        :param isWithAvatar: 消息本身是否显示头像名称
        :return:无
        """
        # 判断是否显示头像
        isShowAvatar = False
        # 若设置显示头像，并且此消息类型也需要显示头像
        if self.style["avatarShow"] == "true" and isWithAvatar:
            # 若消息有头像，且设置了avatarShow则显示头像
            isShowAvatar = True
            startX = self.style["contentStartX"]
        else:
            startX = self.style["leftMargin"]

        # 判断是否显示名称
        isShowName = False
        if self.style["nameShow"] == "true" and isWithAvatar:
            # 若消息有头像，且设置了nameShow则显示名称
            isShowName = True

        # 初始化头像和名称的绘制数据和高度，若不用绘制则会使用初始化的值
        drawDataAva = []
        msgHeightAva = 0
        drawDataName = []
        msgHeightName = 0

        # 发送者信息的起始位置
        senderInfoStartY = self.curY
        SenderInfoHeightSpace = self.curY - self.style["contentMaxY"]

        # 处理名称的绘制数据和高度
        if isShowName:
            isFinishName, msgHeightName, drawDataName = self.dataprocessor.procName(msgData["s"], SenderInfoHeightSpace)
            if not isFinishName:
                # 假如没有绘制成功，需要分页处理
                self.nextPage()
                SenderInfoHeightSpace = self.curY - self.style["contentMaxY"]
                senderInfoStartY = self.curY
                isFinishName, msgHeightName, drawDataName = self.dataprocessor.procName(msgData["s"], SenderInfoHeightSpace)

        # 处理头像的绘制数据和高度
        if isShowAvatar:
            isFinishAva, msgHeightAva, drawDataAva = self.dataprocessor.procAvatar(msgData["s"], SenderInfoHeightSpace)
            if not isFinishAva:
                # 假如没有绘制成功，需要分页处理
                self.nextPage()
                SenderInfoHeightSpace = self.curY - self.style["contentMaxY"]
                senderInfoStartY = self.curY
                isFinishAva, msgHeightAva, drawDataAva = self.dataprocessor.procAvatar(msgData["s"], SenderInfoHeightSpace)
                # msgHeight = max(msgHeight, msgHeightAva)

        # 计算消息内容的起始位置和高度
        heightSpace = self.curY - self.style["contentMaxY"] - msgHeightName
        startY = self.curY - msgHeightName

        # 添加准备绘制细节信息
        drawDataSenderInfo = []
        if drawDataAva != []:
            drawDataSenderInfo.extend(drawDataAva)
        if drawDataName != []:
            drawDataSenderInfo.extend(drawDataName)
        msgHeightSender = max(msgHeightAva, msgHeightName)

        # 绘制不可分割的消息
        if not isDivisible:
            isFinish, msgHeight, drawData = drawFunc(msgData, heightSpace)
            if not isFinish:
                # 假如没有绘制成功，需要分页处理
                self.nextPage()
                heightSpace = self.curY - self.style["contentMaxY"] - msgHeightName
                startY = self.curY - msgHeightName
                senderInfoStartY = self.curY
                isFinish, msgHeight, drawData = drawFunc(msgData, heightSpace)

            # 绘制头像和名称
            if isShowAvatar or isShowName:
                self.drawDataRun(drawDataSenderInfo, startX, senderInfoStartY, self.curC)
            # 绘制消息内容
            self.drawDataRun(drawData, startX, startY, self.curC)
            self.curY -= max(msgHeight + msgHeightName, msgHeightSender)

        # 绘制可分割的消息
        else:
            isFinish = False
            remaindData = msgData["c"]
            while not isFinish:
                isFinish, msgHeight, drawData, isStart, remaindData = drawFunc(
                    remaindData, heightSpace)
                # 绘制头像和名称
                if isStart:
                    # 若在本页/列已经绘制了一部分的消息再绘制头像名称，若没有的话直接换页/列
                    if isShowAvatar or isShowName:
                        self.drawDataRun(drawDataSenderInfo, startX, senderInfoStartY, self.curC)
                # 绘制消息内容
                self.drawDataRun(drawData, startX, startY, self.curC)
                self.curY -= max(msgHeight + msgHeightName, msgHeightSender)
                if not isFinish:
                    # 若没有绘制完成则分页处理
                    self.nextPage()
                    heightSpace = self.curY - self.style["contentMaxY"] - msgHeightName
                    startY = self.curY - msgHeightName
                    senderInfoStartY = self.curY

    def main(self):
        """绘制PDF主函数

        """
        with open(self.paths["outputDirPath"] + "output/chatData.txt", "r") as f:
            i = 1
            total_num = sum(1 for _ in f)
            f.seek(0)
            self.log(f"开始生成：共有{total_num}条")
            current_num = 0
            last_persent = 0

            for line in f:
                obj = json.loads(line)
                i += 1
                persent = int((current_num + 1) / total_num * 100)
                if persent % 5 == 0 and persent != last_persent:
                    last_persent = persent
                    info = f"生成进度：{persent}%, {current_num + 1}/{total_num}"
                    self.log(info)
                current_num += 1

                try:
                    obj["t"]
                except:
                    self.log(f"一条消息出错：{obj}\n")
                    continue

                # print(obj)

                self.procTime(obj["i"])
                try:
                    # 消息类型
                    if obj["t"] == "msg" or obj["t"] == "mixmsg":
                        self.drawMsg(self.dataprocessor.procChatBoxMessage, obj, True, True)

                    if obj["t"] in ["uns", "err", "nudge", "file", "voice"]:
                        self.drawMsg(self.dataprocessor.procDetailMessage, obj, False, True)

                    if obj["t"] in ["uns", "err"]:
                        if obj["c"]["type"] in ["text", "media", "unknown"]:
                            self.drawMsg(self.dataprocessor.procDetailMessage, obj, False, True)
                        elif obj["c"]["type"] in ["tip"]:
                            self.drawMsg(self.dataprocessor.procTipMessage, obj, False, False)

                    elif obj["t"] == "revoke" or obj["t"] == "tip":
                        self.drawMsg(self.dataprocessor.procTipMessage, obj, False, False)

                    elif obj["t"] == "img" or obj["t"] == "video":
                        self.drawMsg(self.dataprocessor.procImgVideoMessage, obj, False, True)

                    elif obj["t"] == "file":
                        self.drawMsg(self.dataprocessor.procDetailMessage, obj, False, True)
                except Exception as e:
                   error_info = traceback.format_exc()
                   self.log(f"一条消息出错：{e}消息为：{obj}\n详细错误信息，建议检查您的设置，并看一看错误排除文档，若无法解决可以上报：{error_info}\n")

                # if i == 80:
                #    break
                self.curY -= self.style["MassageSpacing"]
                if self.curY <= self.style["contentMaxY"]:
                    self.nextPage()

            self.log(f"PDF生成成功\n")



# 为防止设置项被设为小写，使用自己的optionxform函数
def my_optionxform(optionstr: str) -> str:
    return optionstr


class GenerateInit:
    """初始化绘制，加载设置项，初始化绘制层

    """
    def __init__(self, log):
        self.log = log
        pass

    def read_ini_file(self, file_path: str) -> dict:
        """读取给定路径下的INI文件，并返回解析后的字典数据。
        将自动将flt开头的数据转化为float，int同理，其它数据若能转化为数字则转化为mm长度数据

        :param file_path: INI文件的路径
        :return: 解析后的字典数据
        """

        parser = configparser.ConfigParser(allow_no_value=True, inline_comment_prefixes=';', comment_prefixes=';')
        parser.optionxform = my_optionxform

        parser.read(file_path, encoding="utf-8")

        data = {}
        for section in parser.sections():
            for key, value in parser.items(section):
                # 尝试将值转换为 int 类型
                if key[0:3] == "int":
                    value = int(value)
                    data[key] = value
                elif key[0:3] == "flt":
                    value = float(value)
                    data[key] = value
                elif key == "pageSize":
                    if value == 'A4': value = A4
                    if value == 'A5': value = A5
                    data[key] = value

                elif key[0:2] == "mm":
                    value = float(value) * mm
                    data[key[2:]] = value

                else:
                    data[key] = value

        return data

    # 加载style
    def procStyle(self, file_path):
        """加载给定路径下的样式设置文件，计算有关数据，并返回样式设置数据

        :param file_path: 样式文件的路径
        :return: 样式设置字典
        """
        style = self.read_ini_file(file_path)
        # Page
        style["pageHeight"] = style["pageSize"][1]  # 纸张高度
        style["pageWidth"] = style["pageSize"][0] / style["intcolumn"]  # 纸张宽度

        # 聊天内容
        style["contentStartX"] = style["leftMargin"] + \
                                 style["avatarSize"] + 2 * style["avatarMargin"]  # 聊天内容开始X坐标
        style["contentStartY"] = style["pageHeight"] - style["topMargin"]  # 聊天内容开始Y坐标

        style["contentMaxX"] = style["pageWidth"] - style["rightMargin"]  # 聊天内容最大X坐标
        style["contentMaxY"] = style["bottomMargin"] + style["pageFooterTextHeight"]  # 聊天内容最大Y坐标
        style["contentMaxWidth"] = style["pageWidth"] - style["rightMargin"] - style["leftMargin"]
        style["contentCenter"] = style["contentMaxWidth"] / 2 + style["rightMargin"]

        # 聊天框
        style["chatBoxTextStartX"] = style["contentStartX"] + style["chatBoxPadding"]
        style["chatBoxTextMaxX"] = style["contentMaxX"] - style["chatBoxPadding"]
        style["chatBoxTextMaxY"] = style["contentMaxY"] + style["chatBoxPadding"]
        style["chatBoxTextMaxWidth"] = style["chatBoxTextMaxX"] - style["chatBoxTextStartX"]

        # 图像
        style["imgMaxWidth"] = style["pageWidth"] * style["fltimgDrawScale"]
        style["imgMaxHeight"] = style["pageHeight"] * style["fltimgDrawScale"]
        return style

    def readSenderInfo(self, style):
        """读取发送者信息

        :return: 发送者数据字典
        """
        with open('output/senders/senders.json', encoding='utf-8') as f:
            senders = json.load(f)

        if style["emptyAvatarShow"] == "false":
            for key, item in senders.items():
                if not os.path.exists(item[1]):
                    raise FileNotFoundError(f"{key}的头像文件{item[1]}不存在，请补全头像, 或者在设置内关闭显示头像")

        return senders

    def run(self):
        """运行PDF绘制

        """
        if not os.path.exists('config/GeneratePDF_ReportLab_config.ini'):
            self.log(f"设置文件config/GeneratePDF_ReportLab_config.ini不存在")
            return
        try:
            style = self.procStyle('config/GeneratePDF_ReportLab_config.ini')
        except Exception as e:
            self.log(f"读取设置的时候发生错误：{e}")
            return
        try:
            senders = self.readSenderInfo(style)
        except Exception as e:
            self.log(f"读取senders的时候发生错误：{e}")
            return

        print(style)

        fontName = style["fontName"]
        paths = {
            "outputDirPath": ""
        }
        fontPath, fontIndex, fontWarning = fontResolver.resolve_font(fontName)
        if fontWarning:
            self.log(f"⚠️ {fontWarning}")
        paths["fontPath"] = fontPath
        paths["fontIndex"] = fontIndex


        ERRCODE = errcode.ErrCode("print")

        drawingQuery = DrawingQuery(ERRCODE, paths, style, self.log)

        pdfDraw = PdfDraw(ERRCODE, drawingQuery, paths, style, self.log)
        dataprocessor = DataProcessor(ERRCODE, paths, style, senders, pdfDraw, drawingQuery, self.log)
        generate = Generate(ERRCODE, paths, style, pdfDraw, dataprocessor, self.log)
        generate.main()
        pdfDraw.save()

        # 以当前日期时间为文件名
        cur_time = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        dst_file = os.path.join('old', cur_time + ".pdf")

        # # 判断目标目录是否存在，不存在则创建
        # if not os.path.exists('old'):
        #     os.mkdir('old')
        #
        # # 复制文件到目标目录
        # shutil.copy("output/chatData.pdf", dst_file)
