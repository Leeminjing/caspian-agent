/*
本文件对外提供 CaspianImagePaste，含 buildUserMessageContent 等无 DOM 依赖的纯函数，
是「粘贴图片 → OpenAI 兼容 image_url 内容块」构造的唯一实现。

输入为文本 content 与粘贴图片数组（每项 {dataUrl}）；输出为可直接放入 user message 的
content：无图片时返回原文本字符串（向后兼容），有图片时返回 [text 块, ...image_url 块]
列表。

示例：
  const blocks = CaspianImagePaste.buildUserMessageContent("看图", [{ dataUrl: "data:image/png;base64,AA==" }]);
  // → [{ type: "text", text: "看图" }, { type: "image_url", image_url: { url: "data:image/png;base64,AA==" } }]
*/
(function imagePasteModule(global) {
  "use strict";

  function buildUserMessageContent(text, pastedImages = []) {
    const content = String(text || "").trim();
    if (!pastedImages || !pastedImages.length) return content;
    const blocks = [];
    if (content) blocks.push({ type: "text", text: content });
    pastedImages.forEach((img) => {
      if (!img || !img.dataUrl) return;
      blocks.push({ type: "image_url", image_url: { url: img.dataUrl } });
    });
    return blocks;
  }

  global.CaspianImagePaste = { buildUserMessageContent };
})(window);
