# QQ Message Editor

为 AstrBot v4.24.5 编写的私用 OneBot QQ 消息编辑插件。

## 指令

| 指令 | 行为 |
| --- | --- |
| `/resend` | 提示输入新的完整内容 |
| `/resend {新内容}` | 删除最后一轮并将新内容重新投入 AstrBot 正常处理管线 |
| `/edit`、`/edit f` | 返回最后一条 user message 的数据库完整文本 |
| `/edit f {新内容}` | 直接替换最后一条 user message，不触发模型 |
| `/edit a` | 返回最后一条 assistant message 的完整文本 |
| `/edit a {新内容}` | 直接替换最后一条 assistant message，不触发模型 |
| `/patch ...` | 与 `/edit ...` 完全相同 |

指令和目标 `f` / `a` 均不区分大小写。大括号内容支持换行；插件从第一个 `{` 贪婪匹配到消息末尾最后一个 `}`，因此正文中可以包含普通的大括号。

咪——😽