import { useChangeLanguage } from '@/hooks/logic-hooks';
import { Button } from '@/components/ui/button';
import { LanguageAbbreviation } from '@/constants/common';
import { cn } from '@/lib/utils';

export default function LanguageButton({
  language,
  className,
}: {
  language: string;
  className?: string;
}) {
  const changeLanguage = useChangeLanguage();
  const isZh = language === LanguageAbbreviation.Zh;

  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn('relative size-10 shrink-0 lg:size-8 text-xs font-semibold', className)}
      onClick={() => changeLanguage(isZh ? LanguageAbbreviation.En : LanguageAbbreviation.Zh)}
      title={isZh ? 'Switch to English' : '切换到中文'}
    >
      {isZh ? '中' : 'EN'}
    </Button>
  );
}
