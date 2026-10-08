import { AllCommunityModule, ModuleRegistry, themeQuartz } from 'ag-grid-community';
import { AG_GRID_LOCALE_ES } from '@ag-grid-community/locale';

ModuleRegistry.registerModules([AllCommunityModule]);

export const AG_GRID_LOCALE = AG_GRID_LOCALE_ES;

export const gridTheme = themeQuartz.withParams({
  accentColor: 'rgb(150, 0, 72)',
  backgroundColor: '#ffffff',
  borderColor: '#e9e7e8',
  browserColorScheme: 'light',
  foregroundColor: '#3d3d40',
  headerBackgroundColor: '#ffffff',
  headerFontSize: 12,
  headerFontWeight: 600,
  headerTextColor: '#767679',
  headerColumnBorder: { style: 'solid', width: 1, color: '#eeecee' },
  headerRowBorder: { style: 'solid', width: 1, color: '#e9e7e8' },
  columnBorder: { style: 'solid', width: 1, color: '#f1eff0' },
  cellFontSize: 12.5,
  fontFamily: 'inherit',
  oddRowBackgroundColor: '#ffffff',
  rowVerticalPaddingScale: 1.35,
  rowBorder: { style: 'solid', width: 1, color: '#f1eff0' },
  cellHorizontalPadding: 18,
  spacing: 8,
  wrapperBorder: true,
  wrapperBorderRadius: 16,
});
