import hashlib
import io
import os
import sys
import re
import traceback
import json
import shutil
import binascii

# Editor: NoahShen admin@noahshen.top
# Date: 2023/9/14
# Reason: 移除blackboxprotobuf

from PIL import Image

from src.errcode.errcode import ErrCode
from src import mediaTools
from .textParsing import textParsing

sys.path.append("...")
from src.proto import Msg_pb2
from src.proto.RichMsg_pb2 import PicRec
from src.proto.RichMsg_pb2 import Msg

# QQ 表情包文件：前 200 字节与固定 key 异或（SecurityUtile.xorInLimit）
_EMOTICON_XOR_KEY = (0, 1, 0, 1)
_EMOTICON_XOR_LIMIT = 200
_IMAGE_EXT = {"jpeg": "jpg", "mpo": "jpg", "apng": "png"}


def _deobfuscate_emoticon(data):
    buf = bytearray(data)
    for i in range(min(_EMOTICON_XOR_LIMIT, len(buf))):
        buf[i] ^= _EMOTICON_XOR_KEY[i % len(_EMOTICON_XOR_KEY)]
    return bytes(buf)


def _image_extension(data):
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").lower()
    except OSError:
        return ""
    return _IMAGE_EXT.get(fmt, fmt)


def decodeEmoticonFile(data):
    """解出表情包图片内容，返回 (图片字节, 后缀)；无法识别返回 (b"", "")。

    QQ 写盘时会混淆前 200 字节，个别文件可能已是明文，两种都试。
    """
    if len(data) < 32:
        return b"", ""
    plain = _deobfuscate_emoticon(data)
    ext = _image_extension(plain)
    if ext:
        return plain, ext
    ext = _image_extension(data)
    if ext:
        return data, ext
    return b"", ""


_crc64_init = False
_crc64_table = [0] * 256


def crc64(s):
    global _crc64_init
    if not _crc64_init:
        for i in range(256):
            bf = i
            for j in range(8):
                if bf & 1 != 0:
                    bf = bf >> 1 ^ -7661587058870466123
                else:
                    bf >>= 1
            _crc64_table[i] = bf
        _crc64_init = True
    v = -1
    for i in range(len(s)):
        v = _crc64_table[(ord(s[i]) ^ v) & 255] ^ v >> 8
    return v


class protoDataParsing():
    """protobuf序列化相关类型解析

    """

    def __init__(self, errcodeobj: ErrCode, textparsingobj: textParsing, configs):
        self.ERRCODE = errcodeobj
        self.textParsing = textparsingobj
        self.configs = configs

        self.img_md5_map = {}
        self.img_num = 1
        self.video_num = 1
        self.voice_num = 1

    def decodeMarketFace(self, data):
        """解码大表情并移动

        :param data: 数据
        :return: 错误码，文件路径
        """
        emoticon_name = data + ".gif"
        output_path = "output/emoticons/emoticon2/" + emoticon_name
        res_path = "resources/emoticons/emoticon2/" + emoticon_name

        try:
            if os.path.exists(output_path):
                return True, output_path
            elif os.path.exists(res_path):
                shutil.copy(res_path, output_path)
                return True, output_path
            else:
                self.ERRCODE.parse_err("MARKETFACE_NOT_EXIST", [data])
                return False, None
        except OSError:
            self.ERRCODE.parse_err("IO_ERROR", [traceback.format_exc()])
            return False, None

    def marketFaceMsg(self, face_name, ep_id=None, e_id=None):
        """按表情名生成大表情消息块

        :param face_name: 表情名，如 微笑
        :param ep_id: 表情包 id（MarkFaceMessage.dwTabID）
        :param e_id: 表情 id（MarkFaceMessage.sbufID 的十六进制）
        :return: msgOutData
        """
        if not self.configs["needMarketFace"]:
            return {"t": "uns", "c": {"text": "[大表情]", "type": "media"}}
        if ep_id and e_id:
            path = self._copyLocalMarketFace(ep_id, e_id)
            if path:
                return {"t": "img", "c": {"path": path, "name": face_name or e_id, "type": "marketface"}}
            if self.configs.get("lazyImages"):
                # 本地没有表情包文件，交给 Web 端按需从手机回源
                return {"t": "img", "c": {"path": f"lazyface:{ep_id}/{e_id}",
                                          "name": face_name or e_id, "type": "marketface"}}
        if not face_name:
            return {"t": "err", "c": {"text": "[大表情]", "type": "media"}}
        state, path = self.decodeMarketFace(face_name)
        if state:
            return {"t": "img", "c": {"path": path, "name": face_name, "type": "marketface"}}
        return {"t": "err", "c": {"text": "[大表情]", "type": "media"}}

    def _copyLocalMarketFace(self, ep_id, e_id):
        """从本地 .emotionsm/<epId>/<eId> 解出表情包图片并写入 output。"""
        root = self.configs.get("emoticonPath") or ""
        if not root:
            return ""
        source = os.path.join(root, str(ep_id), e_id)
        if not os.path.isfile(source):
            return ""
        try:
            with open(source, "rb") as fh:
                data, ext = decodeEmoticonFile(fh.read())
        except OSError:
            self.ERRCODE.parse_err("IO_ERROR", [traceback.format_exc()])
            return ""
        if not data:
            self.ERRCODE.parse_err("MARKETFACE_NOT_EXIST", [source])
            return ""
        output_path = f"output/emoticons/emoticon2/{e_id}.{ext}"
        try:
            with open(output_path, "wb") as fh:
                fh.write(data)
        except OSError:
            self.ERRCODE.parse_err("IO_ERROR", [traceback.format_exc()])
            return ""
        return output_path

    def video_thumb(self, input_path, output_path):
        try:
            return mediaTools.video_thumbnail(input_path, output_path)
        except mediaTools.MediaToolError:
            self.ERRCODE.parse_err("VIDEO_THUMB_ERROR", [input_path, traceback.format_exc()])
            return ""


    def decodePic(self, data):
        """解码图片并移动

        :param data: 数据
        :return: msgOutData
        """
        if self.configs["needImages"] == False:
            msgOutData = {
                "t": "uns",
                "c": {"text": "[图片]", "type": "media"}
            }
            return msgOutData
        elif self.configs["needImages"] == True:
            msgOutData = {
                "t": "img",
                "c": {"path": "", "type": "", "name": ""}
            }
            try:
                # deserialize_data, message_type = blackboxprotobuf.decode_message(data)
                # print(f"图片原始数据: {deserialize_data}")
                # print(f"消息类型: {message_type}")
                doc = PicRec()
                doc.ParseFromString(data)

                url = 'chatimg:' + doc.md5
                filename = hex(crc64(url))
                filename = 'Cache_' + filename.replace('0x', '')
                relpath = os.path.join(self.configs["imagesPath"], filename[-3:], filename)
                imgPath = os.path.join(filename[-3:], filename)
                # print(doc.uint32_width, doc.uint32_height, doc.uint32_image_type)
                msgOutData["c"]["type"] = "pic"
                msgOutData["c"]["size"] = doc.size
                # msgOutData["c"]["imgType"] = doc.uint32_image_type
                # 数据中，这两项宽高不可靠，请注意！
                # msgOutData["c"]["imgWidth"] = doc.uint32_height
                # msgOutData["c"]["imgHeight"] = doc.uint32_width

            except:
                # protobuf反序列化失败
                self.ERRCODE.parse_err("IMG_DESERIALIZATION_ERROR", [data])
                msgOutData = {"t": "err", "c": {"text": "[图片]", "type": "media"}}
                return msgOutData

            try:
                # 转存图片并去重

                # 判断文件是否存在
                if not os.path.exists(relpath):
                    if self.configs.get("lazyImages"):
                        # 本地没有文件时只记录引用，由 Web 端按需回源
                        msgOutData["c"]["path"] = "lazy:" + doc.md5
                        msgOutData["c"]["name"] = doc.md5
                        return msgOutData
                    msgOutData = {"t": "err", "c": {"text": "[图片]", "type": "media"}}
                    self.ERRCODE.parse_err("IMG_NOT_EXIST", [relpath])
                    return msgOutData

                # 计算图片的MD5值
                with open(relpath, 'rb') as f:
                    img_data = f.read()
                    md5 = hashlib.md5(img_data).hexdigest()

                # 查找图片的MD5值是否已经存在
                if md5 in self.img_md5_map:
                    msgOutData["c"]["path"] = self.img_md5_map[md5][0]
                    msgOutData["c"]["name"] = self.img_md5_map[md5][1]
                    return msgOutData

                # 确定图片类型并添加后缀名
                try:
                    with Image.open(relpath) as img:
                        img_type = img.format.lower() if img.format else None
                except OSError:
                    img_type = None
                # JPEG 变体（MPO/JFIF）统一按 jpeg 处理
                if img_type in ("mpo", "jfif"):
                    img_type = "jpeg"
                if img_type is None:
                    msgOutData = {"t": "err", "c": {"text": "[图片]", "type": "media"}}
                    self.ERRCODE.parse_err("IMG_UNKNOWN_TYPE_ERROR", [imgPath])
                    return msgOutData

                new_filename = f'{self.img_num}.{img_type}'
                self.img_num = self.img_num + 1
                new_file_path = os.path.join('output', 'images', new_filename)

                # 移动图片文件到output/images文件夹中，并重命名
                try:
                    shutil.copy(relpath, new_file_path)
                except:
                    msgOutData = {"t": "err", "c": {"text": "[图片]", "type": "media"}}
                    self.ERRCODE.parse_err("IO_ERROR", [relpath, new_file_path])
                    return msgOutData

                # 将MD5和新文件路径添加到imgMD5Map中
                self.img_md5_map[md5] = [new_file_path, new_filename]

                msgOutData["c"]["path"] = new_file_path
                msgOutData["c"]["name"] = new_filename
                return msgOutData
            except OSError:
                msgOutData = {"t": "err", "c": {"text": "[图片]", "type": "media"}}
                self.ERRCODE.parse_err("IO_ERROR", [traceback.format_exc()])
                return msgOutData

    def decodeMixMsg(self, data):
        """解码混合消息

        :param data: 数据
        :return: 错误码， msgList
        """
        msgList = []
        try:
            doc = Msg()
            doc.ParseFromString(data)
            for elem in doc.elems:
                if elem.picMsg:
                    msgList.append(self.decodePic(elem.picMsg))
                else:
                    msgText = elem.textMsg.decode('utf-8')
                    if msgText != " ":
                        for msgElem2 in self.textParsing.parse(msgText):
                            msgList.append(msgElem2)
            return True, msgList
        except:
            self.ERRCODE.parse_err("MIXMSG_DESERIALIZATION_ERROR", [data, traceback.format_exc()])
            return False, None

    def decodeVoiceMsg(self, data):
        """
        解码移动视频消息
        :param data: proto数据
        :return:
        """
        if self.configs["needVoice"] == False:
            msgOutData = {
                "t": "uns",
                "c": {"text": "[语音]", "type": "media"}
            }
        elif self.configs["needVoice"] == True:
            msgOutData = {
                "t": "voice",
                "c": {}
            }
            try:
                doc = Msg_pb2.Voice()
                doc.ParseFromString(data)
                file_path = doc.field1.decode("utf-8")
                voice_length = doc.field19
                # print(file_path, voice_length)
            except:
                self.ERRCODE.parse_err("VOICE_DESERIALIZATION_ERROR", [data])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData



            pattern = r'/([^/]+)\.\w+$'
            match = re.search(pattern, file_path)
            if not match:
                self.ERRCODE.parse_err("VOICE_UNKNOWN_PATH_FORMAT", [file_path])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData

            relpath = self.configs["voicePath"] + "\\" + match.group(1)

            if os.path.exists(relpath + ".amr"):
                relpath = relpath + ".amr"
            elif os.path.exists(relpath + ".slk"):
                relpath = relpath + ".slk"
            else:
                self.ERRCODE.parse_err("VOICE_NOT_EXIST", [relpath, file_path])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData

            try:
                with open(relpath, "rb") as file:
                    header_length = 16
                    file_header = file.read(header_length)

            except:
                self.ERRCODE.parse_err("IO_ERROR", [relpath])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData

            new_filename = f'{self.voice_num}.mp3'
            new_file_path = os.path.join('output', 'voices', new_filename)
            self.voice_num += 1
            temp_path = os.path.join('output', 'temp', 'voice.pcm')

            def starts_with_bytes(bytesA, bytesB):
                if len(bytesA) < len(bytesB):
                    return False
                return bytesA[:len(bytesB)] == bytesB

            is_silk = starts_with_bytes(file_header, bytes.fromhex("02232153494c4b5f"))
            is_amr = starts_with_bytes(file_header, bytes.fromhex("2321414d52"))
            if not is_silk and not is_amr:
                self.ERRCODE.parse_err("VOICE_UNKNOWN_FILEHEADER", [file_path, binascii.hexlify(file_header).decode('utf-8')])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData

            try:
                if is_silk:
                    # SILK_V3
                    mediaTools.silk_to_pcm(relpath, temp_path)
                    mediaTools.pcm_to_mp3(temp_path, new_file_path)
                else:
                    # AMR
                    mediaTools.audio_to_mp3(relpath, new_file_path)
            except mediaTools.MediaToolError as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                self.ERRCODE.parse_err("VOICE_CONVERT_ERROR", [relpath, str(e)])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData

            if not os.path.exists(new_file_path):
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                self.ERRCODE.parse_err("VOICE_CONVERT_ERROR", [relpath])
                msgOutData = {"t": "err", "c": {"text": "[语音]", "type": "media"}}
                return msgOutData

            if os.path.exists(temp_path):
                os.remove(temp_path)

            msgOutData["c"]["path"] = new_file_path
            msgOutData["c"]["length"] = voice_length
            return msgOutData



    def decodeVideoMsg(self, data):
        """
        解码移动视频消息
        :param data: proto数据
        :return:
        """
        if self.configs["needVideo"] == False:
            msgOutData = {
                "t": "uns",
                "c": {"text": "[视频]", "type": "media"}
            }
        elif self.configs["needVideo"] == True:
            msgOutData = {
                "t": "video",
                "c": {}
            }
            try:
                doc = Msg_pb2.ShortVideo()
                doc.ParseFromString(data)
                filePath = doc.field3.decode("utf-8")
                # deserialize_data, message_type = blackboxprotobuf.decode_message(data)
                # print(f"原始数据: {deserialize_data}")
                # print(f"消息类型: {message_type}")
            except:
                self.ERRCODE.parse_err("VIDEO_DESERIALIZATION_ERROR", [data])
                msgOutData = {"t": "err", "c": {"text": "[视频]", "type": "media"}}
                return msgOutData

            pattern = r'/([A-F0-9]{32})/'
            match = re.search(pattern, filePath)
            if not match:
                self.ERRCODE.parse_err("VIDEO_UNKNOWN_PATH_FORMAT", [filePath])
                msgOutData = {"t": "err", "c": {"text": "[视频]", "type": "media"}}
                return msgOutData

            # 判断文件是否存在
            relpath = self.configs["videoPath"] + "\\" + match.group(1)
            if not os.path.exists(relpath):
                self.ERRCODE.parse_err("VIDEO_NOT_EXIST", [relpath])
                msgOutData = {"t": "err", "c": {"text": "[视频]", "type": "media"}}
                return msgOutData

            file_count = 0
            found_file = None
            for root, dirs, files in os.walk(relpath):
                for file in files:
                    if file != '.nomedia':
                        file_count += 1
                        found_file = os.path.join(root, file)

            if file_count == 0:
                self.ERRCODE.parse_err("VIDEO_NOT_EXIST", [relpath, file_count])
                msgOutData = {"t": "err", "c": {"text": "[视频]", "type": "media"}}
                return msgOutData
            elif file_count > 1:
                self.ERRCODE.parse_err("VIDEO_NOT_EXIST", [relpath, file_count])
                msgOutData = {"t": "err", "c": {"text": "[视频]", "type": "media"}}
                return msgOutData

            _, file_extension = os.path.splitext(found_file)
            new_filename = f'{self.video_num}{file_extension}'
            new_picname = f'{self.video_num}.jpg'
            self.video_num += 1
            new_file_path = os.path.join('output', 'videos', new_filename)
            new_pic_path = os.path.join('output', 'videos', 'thumbs', new_picname)
            # 移动图片文件到output/videos文件夹中，并重命名
            try:
                shutil.copy(found_file, new_file_path)
            except:
                self.ERRCODE.parse_err("IO_ERROR", [found_file, new_file_path])
                msgOutData = {"t": "err", "c": {"text": "[视频]", "type": "media"}}
                return msgOutData

            msgOutData["c"]["path"] = new_file_path
            msgOutData["c"]["name"] = new_filename

            msgOutData["c"]["thumb"] = self.video_thumb(new_file_path, new_pic_path)
            return msgOutData

    def parse(self, msgType, msgData, extStr):
        """protobuf序列化相关类型解析

        :param msgType: 消息类型
        :param msgData: 数据
        :param extStr:  extStr
        :return: msgOutData
        """
        msgOutData = {}

        # 图片类型
        if msgType == -2000:
            msgOutData = self.decodePic(msgData)
            # print(extStr)


        # 图文混排
        elif msgType == -1035:
            state, msgDeseData = self.decodeMixMsg(msgData)
            if state:
                msgOutData = {
                    "t": "mixmsg",
                    "c": msgDeseData
                }
            else:
                msgOutData = {"t": "err", "c": {"text": "[混合消息]", "type": "text"}}


        # 语音
        elif msgType == -2002:
            msgOutData = self.decodeVoiceMsg(msgData)
            return msgOutData

        # 短视频
        elif msgType == -2022:
            msgOutData = self.decodeVideoMsg(msgData)
            return msgOutData


        # 大号表情
        elif msgType == -8018:
            if not self.configs["needMarketFace"]:
                msgOutData = self.marketFaceMsg("")
            else:
                try:
                    doc = Msg_pb2.marketFace()
                    doc.ParseFromString(msgData)
                    marketFaceName = doc.field7.decode("utf-8")[1:]
                    msgOutData = self.marketFaceMsg(marketFaceName)
                except Exception:
                    msgOutData = {"t": "err", "c": {"text": "[大表情]", "type": "media"}}
                    self.ERRCODE.parse_err("MARKETFACE_DESERIALIZATION_ERROR", [msgType, msgData])

            return msgOutData


        elif msgType == -5040:  # 灰条消息
            extStrJson = json.loads(extStr) if extStr else {}

            if "revoke_op_type" in extStrJson.keys():
                # 自己撤回
                if extStrJson["revoke_op_type"] == "0":
                    doc = Msg_pb2.grayTipBar()
                    doc.ParseFromString(msgData)
                    msgText = doc.field5.decode("utf-8")
                    # 撤回消息
                    msgOutData = {
                        "t": "revoke",
                        "c": {"text": msgText, "type": "by_self"}}
                    # print(extStr)
                # 群主（或管理员，待验证）撤回
                elif extStrJson["revoke_op_type"] == "2":
                    doc = Msg_pb2.grayTipBar()
                    doc.ParseFromString(msgData)
                    msgText = doc.field5.decode("utf-8")
                    msgOutData = {
                        "t": "revoke",
                        "c": {"text": msgText, "type": "by_admin"}
                    }
                    # print(extStr)
                else:
                    self.ERRCODE.parse_err("DIDNOT_PARSE_MSG", [msgType, msgData, extStr])
                    msgOutData = {
                        "t": "uns",
                        "c": {"text": "[提示消息]", "type": "tip"}
                    }

            # 被邀请进入群聊
            elif ("inviteeUin" in extStrJson.keys()) or ("invitorUin" in extStrJson.keys()):
                doc = Msg_pb2.grayTipBar()
                doc.ParseFromString(msgData)
                msgDecodedData = doc.field5.decode("utf-8")
                # print(doc.field5.decode("utf-8"))
                if ("inviteeUin" in extStrJson.keys()) and ("invitorUin" in extStrJson.keys()):
                    inviteeUin = extStrJson["inviteeUin"]
                    invitorUin = extStrJson["invitorUin"]
                    items = {
                        "inviteeUin": inviteeUin,
                        "invitorUin": invitorUin
                    }
                    msgOutData = {
                        "t": "tip",
                        "c": {"text": msgDecodedData, "type": "invite", "ext": items}
                    }
                else:
                    msgOutData = {
                        "t": "tip",
                        "c": {"type": "invite"}
                    }
                    self.ERRCODE.parse_err("EXTSTR_NOT_EXIST_TARGET", [extStr, "invitee or invitor "])


            # busi_type
            elif "uint64_busi_type" in extStrJson.keys():
                busi_type = extStrJson["uint64_busi_type"]

                # 签到打卡
                if busi_type == "14" and busi_type == "1":
                    doc = Msg_pb2.grayTipBar()
                    doc.ParseFromString(msgData)

                    msgDecodedData = doc.field5.decode("utf-8")
                    # print(msgType, 14, doc.field5.decode("utf-8"))
                    msgOutData = {
                        "t": "tip",
                        "c": {"text": msgDecodedData, "type": "dailyattendance"}
                    }

                # 灰条戳一戳
                elif busi_type == "12":
                    doc = Msg_pb2.grayTipBar()
                    doc.ParseFromString(msgData)
                    msgDecodedData = doc.field5.decode("utf-8")
                    # print(msgType, 12,  extStrJson["bytes_content"])
                    # print(msgType, 12, doc.field5.decode("utf-8"))
                    msgOutData = {
                        "t": "tip",
                        "c": {"text": msgDecodedData, "type": "pai_yi_pai"}
                    }

                else:
                    doc = Msg_pb2.grayTipBar()
                    doc.ParseFromString(msgData)
                    msgDecodedData = doc.field5.decode("utf-8")
                    self.ERRCODE.parse_err("DIDNOT_PARSE_MSG", [msgType, msgData,msgDecodedData, extStr])
                    msgOutData = {
                        "t": "tip",
                        "c": {"text": msgDecodedData, "type": "unknown"}
                    }

            else:
                doc = Msg_pb2.grayTipBar()
                doc.ParseFromString(msgData)
                msgDecodedData = doc.field5.decode("utf-8")
                self.ERRCODE.parse_err("DIDNOT_PARSE_MSG", [msgType, msgData,msgDecodedData, extStr])
                msgOutData = {
                    "t": "tip",
                    "c": {"text": msgDecodedData, "type": "unknown"}
                }



        # 群标识卡片,戳一戳
        elif msgType == -5020:
            doc = Msg_pb2.grayTipBar()
            doc.ParseFromString(msgData)
            msgDecodedData = doc.field5.decode("utf-8")
            self.ERRCODE.parse_err("DIDNOT_PARSE_MSG", [msgType, msgData, msgDecodedData, extStr])
            msgOutData = {
                "t": "tip",
                "c": {"text": msgDecodedData, "type": "unknown"}
            }

        elif msgType == -5023:  # 该用户通过***向你发起临时会话，前往设置。
            # deserialize_data, message_type = blackboxprotobuf.decode_message(msgData)
            # print(f"-5023原始数据: {deserialize_data}")
            # print(f"消息类型: {message_type}")
            pass

        return msgOutData
