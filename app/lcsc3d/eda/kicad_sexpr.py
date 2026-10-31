"""Read KiCad S-expressions while retaining the exact original document."""
from dataclasses import dataclass
import json
import re

from lcsc3d.core.errors import DownloadError


@dataclass
class Atom:
    value: str
    start: int
    end: int
    quoted: bool = False


@dataclass
class Node:
    values: list
    start: int
    end: int = 0

    def atom(self, index=0):
        if index >= len(self.values) or not isinstance(self.values[index], Atom):
            raise DownloadError('KiCad 文件字段不完整，未修改原文件')
        return self.values[index].value

    def children(self, kind):
        return [value for value in self.values if isinstance(value, Node) and value.atom() == kind]


TOKEN = re.compile(r'\s+|\#[^\r\n]*|"(?:\\[\s\S]|[^"\\])*"|[()]|[^\s()"#]+')


class Document:
    def __init__(self, payload, kind=None, check=lambda: None):
        try:
            self.text = payload.decode('utf-8-sig')
        except UnicodeError as exc:
            raise DownloadError('KiCad 文件必须使用 UTF-8 编码') from exc
        self.bom = payload.startswith(b'\xef\xbb\xbf')
        stack, roots, position = [], [], 0
        for index, match in enumerate(TOKEN.finditer(self.text)):
            if index % 1024 == 0:
                check()
            if match.start() != position:
                raise DownloadError('KiCad 文件字符串未闭合')
            position = match.end()
            token = match[0]
            if token.isspace() or token.startswith('#'):
                continue
            if token == '(':
                node = Node([], match.start())
                (stack[-1].values if stack else roots).append(node)
                stack.append(node)
                if len(stack) > 128:
                    raise DownloadError('KiCad 文件嵌套过深')
            elif token == ')':
                if not stack or not stack[-1].values:
                    raise DownloadError('KiCad 文件括号不匹配')
                stack.pop().end = match.end()
            else:
                if not stack:
                    raise DownloadError('KiCad 文件格式无法识别')
                try:
                    value = json.loads(token, strict=False) if token.startswith('"') else token
                except ValueError as exc:
                    raise DownloadError('KiCad 文件字符串无效') from exc
                stack[-1].values.append(Atom(value, match.start(), match.end(), token.startswith('"')))
        if position != len(self.text) or stack or len(roots) != 1:
            raise DownloadError('KiCad 文件括号或字符串未闭合')
        self.root = roots[0]
        if kind and self.root.atom() != kind:
            raise DownloadError('KiCad 文件类型不匹配，未修改原文件')

    def source(self, node):
        return self.text[node.start:node.end]

    def insert(self, nodes):
        if not nodes:
            return self.encode(self.text)
        newline = '\r\n' if '\r\n' in self.text else '\n'
        position = self.root.end - 1
        addition = newline + newline.join(nodes) + newline
        return self.encode(self.text[:position] + addition + self.text[position:])

    def encode(self, text):
        return (b'\xef\xbb\xbf' if self.bom else b'') + text.encode('utf-8')
