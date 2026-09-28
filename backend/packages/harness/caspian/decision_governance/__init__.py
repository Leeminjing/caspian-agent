"""本文件声明决策表治理包，包内对外提供领域、权限、操作与持久化模块。

输入为已验证的会话身份、候选决策行及基准修订；输出为可审计操作和权威表快照。
工作流由纯领域计算、权限判定和原子仓储提交组成。
示例：`from caspian.decision_governance.domain import TableSnapshot`。
"""
