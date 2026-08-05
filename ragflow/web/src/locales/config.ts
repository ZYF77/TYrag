import { LanguageAbbreviation } from '@/constants/common';
import storage from '@/utils/authorization-util';
import dayjs from 'dayjs';
import i18n from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { initReactI18next } from 'react-i18next';
import translation_en from './en';

const languageImports: Record<string, () => Promise<{ default: any }>> = {
  [LanguageAbbreviation.Zh]: () => import('./zh'),
  [LanguageAbbreviation.En]: () => import('./en'),
};

const supportedLanguageCodes: Intl.UnicodeBCP47LocaleIdentifier[] =
  Object.keys(languageImports);

export const supportedLanguages = supportedLanguageCodes.map((code) => ({
  code,
  displayName: code === LanguageAbbreviation.Zh ? '简体中文' : 'English',
}));

export const DEFAULT_LANGUAGE_CODE =
  import.meta.env.VITE_DEFAULT_LANGUAGE_CODE || LanguageAbbreviation.Zh;

const resources = {
  [LanguageAbbreviation.En]: translation_en,
};

const updateDocumentLocale = (lng: string) => {
  document.documentElement.lang = lng;
  document.documentElement.dir = 'ltr';
  dayjs.locale(lng === 'zh' ? 'zh-cn' : lng);
};

i18n
  .use(initReactI18next)
  .use(LanguageDetector)
  .init({
    detection: {
      lookupLocalStorage: 'lng',
      order: ['localStorage'],
      caches: [],
    },
    supportedLngs: supportedLanguageCodes,
    resources,
    fallbackLng: DEFAULT_LANGUAGE_CODE,
    interpolation: {
      escapeValue: false,
    },
  });

export const loadLanguageAsync = async (lng: string): Promise<void> => {
  const normalizedLng = lng;

  if (i18n.hasResourceBundle(normalizedLng, 'translation')) {
    return;
  }

  const importFn = languageImports[normalizedLng];
  if (!importFn) {
    console.warn('Language ' + lng + ' is not supported for lazy loading');
    return;
  }

  try {
    const module = await importFn();
    const translationData = module.default?.translation || module.default;
    i18n.addResourceBundle(normalizedLng, 'translation', translationData);
  } catch (error) {
    console.error('Failed to load language ' + lng + ':', error);
  }
};

export const changeLanguageAsync = async (lng: string): Promise<void> => {
  const normalizedLng = lng;

  if (
    normalizedLng !== LanguageAbbreviation.En &&
    !i18n.hasResourceBundle(normalizedLng, 'translation')
  ) {
    await loadLanguageAsync(normalizedLng);
  }

  storage.setLanguage(lng);

  updateDocumentLocale(lng);

  await i18n.changeLanguage(normalizedLng);
};

export const initLanguage = async (): Promise<void> => {
  const currentLng = storage.getLanguage() || DEFAULT_LANGUAGE_CODE;

  await changeLanguageAsync(currentLng);
};

export default i18n;
