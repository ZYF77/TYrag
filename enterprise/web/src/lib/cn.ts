/** 过滤 falsy 片段后用空格连接，替代手写的模板字符串 className 拼接。 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
