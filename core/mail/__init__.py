"""邮件领域原语：账号配置、SMTP 发送、IMAP 只读查询、正文与附件处理。

接入端（``interfaces/email``）与 ``mail`` 工具共用这一层，避免 core 反向依赖
interfaces。各子模块按需直接导入，包本身不产生副作用。
"""