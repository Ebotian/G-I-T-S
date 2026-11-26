"""真相与投影：事件是唯一真相，其余都可重建。

    db.py      单写者、迁移、WAL
    events.py  日志集、稳定身份、导出导入
    project.py 恢复重放（**不调模型**，缺结果就抛 MissingModelResult）
    scope.py   可见域——一个门决定"谁看得见什么"
    replica.py 单向副本，拉取式、断点续传
    sync.py    分层同步流
    wipe.py    清空内容、保留机制
"""
