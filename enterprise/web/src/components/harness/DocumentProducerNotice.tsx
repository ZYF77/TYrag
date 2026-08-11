export function DocumentProducerNotice() {
  return (
    <section aria-label="服务侧文档 producer" className="rounded-xl border border-amber-200 bg-amber-50 p-4 shadow-sm">
      <h2 className="text-sm font-semibold text-amber-900">文档同步：服务侧 producer</h2>
      <p className="mt-2 text-xs leading-5 text-amber-800">
        Gateway/demo 模式的浏览器只验证用户 JWT 会话与真 SSE；文档事件和状态接口是 HMAC-only，必须由服务端或 CLI producer 调用。
      </p>
      <p className="mt-2 text-[11px] leading-5 text-amber-700">
        本页面不会把用户 Bearer 当作文档服务主体，也不会持有或发送 HMAC secret。请按 M1-E 运行说明启动 producer。
      </p>
    </section>
  );
}
