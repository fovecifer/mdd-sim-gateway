# 架构说明

MDD 用“物理设备”作为用户可见边界。每个蜂窝模块拥有独立 ModemManager 对象、数据 bearer、SIM 桥和 VoWiFi 实例；普通读卡器没有射频能力，因此不会显示可用的 4G 开关。

控制面运行 FastAPI 与 React，保存期望状态并展示实际状态。宿主机 orchestrator 管理 USB 拓扑、模块 SIM 桥、ModemManager/NetworkManager 及 sing-box TUN。模块 SIM 桥通过 ModemManager 的命令接口传递 APDU；安装程序会启用该接口，并立即把运行日志级别降回 INFO。每张已配置 SIM 对应一个隔离 Docker 引擎，内部运行修改后的 SWu IKEv2/IPsec 客户端和 sysmocom Asterisk。

插入身份可读的新 SIM 时，控制面会按 ICCID 自动创建一条停止状态的线路草稿并绑定稳定读卡器端口；MCC 可直接确定国家出口。只有读卡器无法提供的必填信息（例如外接 PC/SC 读卡器没有硬件 IMEI）需要用户补充，完成后同一草稿转为正式线路并启动，不会覆盖其他 SIM 线路。

4G、飞行模式与 VoWiFi 是三个独立状态。4G 开关只建立或断开该模块的 NetworkManager 移动数据承载；飞行模式通过 ModemManager 单独关闭或开启模块射频；VoWiFi 独立启停。关闭 4G 不再停止 ModemManager，也不会隐式进入飞行模式。一个能力失败不会伪造另一个能力失败。每个物理模块映射到独立 ModemManager 对象和 NetworkManager 连接，状态与流量按模块读取；普通读卡器只显示 VoWiFi，不显示 4G 或飞行模式。

每张模块 UICC 最多使用逻辑通道 1–3，三条通道分别保留给 PIN 保活、SWu/EAP-AKA 和 IMS/Asterisk；这是每张物理 SIM 的独立上限，不是多模块共享的全局池。桥接器在元数据中发布容量、实际通道号、用途和分配状态。启动时若只分配了部分通道或收到重复通道，会先释放本轮已经打开的通道，再发布明确错误并退出，由编排器按正常恢复流程重试。状态机以飞行模式、4G 数据和 VoWiFi 三个布尔输入形成八种组合，并通过纯函数表驱动测试约束有效射频、数据承载和 SIM 桥目标。

国家出口以 SIM MCC 或线路覆盖值选择。配置分为可复用的代理库与国家绑定：订阅节点按名称关键词进入选择池，具体节点和 SOCKS5 则直接绑定。sing-box 继续拥有每国 TUN、路由和选择器；VLESS Reality/XHTTP 由仅监听回环地址的 Xray-core SOCKS 端点承载，再作为 sing-box 的上游。所有路径都进行运行时 UDP 验证。没有健康出口时仅阻止对应 VoWiFi 线路启动，4G 数据保持独立。

AnyTLS 可通过原始 sing-box outbound JSON 或已有 outbound 接入（需要 sing-box 1.12+），直接由国家出口进程承载，无需额外 SOCKS 转接服务。此入口保留原始 TLS/SNI 和会话参数，仍须通过内核配置校验和 UDP 测试；显式限制为 TCP 的配置仍被拒绝。此支持不包含 AnyTLS 分享链接或 Clash 订阅格式转换。
