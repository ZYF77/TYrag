export function DocumentProducerNotice() {
  return (
    <section aria-label="服务侧文档 producer" className="console-card">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Producer</p>
          <h2>文档同步：服务侧 producer</h2>
          <p>Gateway/demo 模式的浏览器只验证用户 JWT 会话与真 SSE；文档事件和状态接口是 HMAC-only，必须由服务端或 CLI producer 调用。</p>
        </div>
      </div>
      <p className="console-note">
        本页面不会把用户 Bearer 当作文档服务主体，也不会持有或发送 HMAC secret。请按 M1-E 运行说明启动 producer。
      </p>
    </section>
  );
}
