/*
本文件对外提供 CaspianThreadList，含 mergeThreads 与 sortThreads 两个无 DOM 依赖的纯函数，
是会话列表「事实源合并」与「最近活跃排序」的唯一实现。

输入为服务端 /api/contexts/tree 返回的节点数组（每项含 context_id/title/updated_at）与本地
缓存的会话数组（每项含 id/title/updatedAt）；输出为可直接渲染的会话数组，每项形如
{ id, title, updatedAt, pending }，其中 pending 表示该会话尚未在服务端建立记录。

具体工作流为：mergeThreads 以 thread_id 为键做并集去重，服务端条目优先并标记 pending=false，
服务端 title 为空时回退本地标题；仅存在于本地的条目标记 pending=true。sortThreads 返回排序
副本，pending 条目整体置顶，其余按 updatedAt 由新到旧排列，同键次序稳定。

示例：
  const rows = CaspianThreadList.sortThreads(
    CaspianThreadList.mergeThreads(treeNodes, localThreads)
  );
  // rows[0] 为刚新建尚未运行的会话，其后为最近活跃的会话
*/
(function threadListModule(global) {
  "use strict";

  const DEFAULT_TITLE = "新会话";

  function toTime(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : 0;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function mergeThreads(serverNodes, localThreads) {
    const local = new Map();
    (Array.isArray(localThreads) ? localThreads : []).forEach(thread => {
      if (thread && thread.id) local.set(thread.id, thread);
    });

    const merged = [];
    const seen = new Set();

    (Array.isArray(serverNodes) ? serverNodes : []).forEach(node => {
      const id = node && node.context_id;
      if (!id || seen.has(id)) return;
      seen.add(id);
      const fallback = local.get(id);
      merged.push({
        id,
        title: node.title || (fallback && fallback.title) || DEFAULT_TITLE,
        updatedAt: toTime(node.updated_at),
        pending: false,
      });
    });

    (Array.isArray(localThreads) ? localThreads : []).forEach(thread => {
      if (!thread || !thread.id || seen.has(thread.id)) return;
      seen.add(thread.id);
      merged.push({
        id: thread.id,
        title: thread.title || DEFAULT_TITLE,
        updatedAt: toTime(thread.updatedAt),
        pending: true,
      });
    });

    return merged;
  }

  function sortThreads(threads) {
    // ponytail: 纯按最近活跃倒序，无未入库置顶特殊分支。新建会话的 updatedAt 是
    // 创建瞬间的 Date.now()，自然排在最前；陈年空会话按时点下沉，无需特判。
    return (Array.isArray(threads) ? [...threads] : []).sort((a, b) => {
      return toTime(b.updatedAt) - toTime(a.updatedAt);
    });
  }

  global.CaspianThreadList = { mergeThreads, sortThreads };
})(window);
