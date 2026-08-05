import { IconFontFill } from '@/components/icon-font';
import { RAGFlowAvatar } from '@/components/ragflow-avatar';
import { Button } from '@/components/ui/button';
import {
  useFetchUserInfo,
  useListTenant,
} from '@/hooks/use-user-setting-request';
import { cn } from '@/lib/utils';
import { TenantRole } from '@/pages/user-setting/constants';
import { Routes } from '@/routes';
import React, { useMemo } from 'react';
import { Link, useLocation } from 'react-router';
import { BellButton } from './bell-button';
import { DesktopNavbar, MobileNavbar } from './global-navbar';
import { MobileMenuFooter } from './mobile-menu-footer';
import LanguageButton from './language-button';
import ThemeButton from './theme-button';
import { useHeaderNavLayout } from './use-header-nav-layout';

export function Header({
  className,
  ...props
}: React.HTMLAttributes<HTMLElement>) {
  const { pathname } = useLocation();

  const {
    data: { language = 'zh-Hans', avatar, nickname },
  } = useFetchUserInfo();

  const { data: tenantData } = useListTenant();
  const hasNotification = useMemo(
    () => tenantData?.some((x) => x.role === TenantRole.Invite),
    [tenantData],
  );

  const {
    headerRef,
    logoRef,
    expandedRightMeasureRef,
    navMeasureRef,
    isCompact,
  } = useHeaderNavLayout(${hasNotification}-);

  return (
    <>
      <header
        ref={headerRef}
        key="app-navbar"
        className={cn(
          'w-full min-w-0 flex items-center gap-2 sm:gap-4',
          className,
        )}
        {...props}
      >
        <div className="inline-flex shrink-0 items-center gap-2">
          {isCompact && (
            <MobileNavbar
              renderFooter={(close) => <MobileMenuFooter onClose={close} />}
            />
          )}
          <div ref={logoRef} className="inline-flex shrink-0 items-center">
            <Link
              to={Routes.Root}
              aria-current={pathname === Routes.Root ? 'page' : undefined}
              className="flex size-10 shrink-0 items-center justify-center"
            >
              <img src={'/logo.svg'} alt="RAGFlow logo" className="size-10" />
            </Link>
          </div>
        </div>

        {!isCompact && (
          <div className="flex min-w-0 flex-1 justify-center overflow-hidden">
            <DesktopNavbar />
          </div>
        )}

        {isCompact && <div className="flex-1" aria-hidden />}

        <div
          className={cn(
            'flex shrink-0 items-center justify-end text-text-badge',
            isCompact ? 'gap-0.5' : 'gap-4',
          )}
          data-testid="auth-status"
        >
          <LanguageButton language={language} className={cn(!isCompact && '!size-8')} />

          <ThemeButton className={cn(!isCompact && '!size-8')} />

          {!isCompact && hasNotification && <BellButton className="!size-8" />}

          <Link
            to={Routes.UserSetting}
            className={cn(
              'relative flex size-10 shrink-0 items-center justify-center',
              !isCompact && 'ms-3',
            )}
            data-testid="settings-entrypoint"
          >
            <RAGFlowAvatar
              name={nickname}
              avatar={avatar}
              isPerson
              className="size-8"
            />
          </Link>
        </div>
      </header>

      <div
        className="pointer-events-none invisible fixed -left-[9999px] top-0"
        aria-hidden
      >
        <div ref={navMeasureRef}>
          <DesktopNavbar />
        </div>
        <div
          ref={expandedRightMeasureRef}
          className="inline-flex shrink-0 items-center justify-end gap-4 text-text-badge"
        >
          <LanguageButton language={language} />
          <ThemeButton className="!size-8" />
          {hasNotification && <BellButton className="!size-8" />}
          <div className="relative ms-3 flex size-10 shrink-0 items-center justify-center">
            <RAGFlowAvatar
              name={nickname}
              avatar={avatar}
              isPerson
              className="size-8"
            />
          </div>
        </div>
      </div>
    </>
  );
}
