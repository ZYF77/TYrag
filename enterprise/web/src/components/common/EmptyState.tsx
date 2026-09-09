/**
 * 列表空态：加载中与无数据共用一个 <p className="console-empty"> 插槽，文案由调用方传入。
 */
export function EmptyState({
  loading,
  loadingText,
  emptyText,
}: {
  loading: boolean;
  loadingText: string;
  emptyText: string;
}) {
  return <p className="console-empty">{loading ? loadingText : emptyText}</p>;
}
