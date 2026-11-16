import { usePdfcnTheme, useSafeMemo } from "@/components/pdf/theme-provider";
import { Text, StyleSheet, View } from "@/lib/pdf-primitives";
import type { Style } from "@/lib/pdf-primitives";
import type { PDFComponentProps } from "@/types/pdf-components";
import type { PdfcnTheme } from "@/types/pdf-themes";

export type PageNumberAlign = "left" | "center" | "right";
export type PageNumberSize = "xs" | "sm" | "md";

/**
 * Auto page number rendered with a configurable format string at a or inline position.
 * Props - `format` | `align` | `size` | `fixed` | `muted` | `style`
 * @see {@link PageNumberProps}
 */
export interface PageNumberProps extends Omit<PDFComponentProps, "children"> {
  /**
   * Format string — use `{page}` for current page and `{total}` for total page count.
   * @default 'Page {page} of {total}'
   */
  format?: string;
  /**
   * @default 'center'
   */
  align?: PageNumberAlign;
  /**
   * @default 'sm'
   */
  size?: PageNumberSize;
  /**
   * @default false
   */
  fixed?: boolean;
  /**
   * @default true
   */
  muted?: boolean;
  children?: never;
}

const createPageNumberStyles = (t: PdfcnTheme) => {
  const { typography, colors, primitives } = t;
  return StyleSheet.create({
    alignCenter: { textAlign: "center" },
    alignLeft: { textAlign: "left" },
    alignRight: { textAlign: "right" },
    colorForeground: { color: colors.foreground },
    colorMuted: { color: colors.mutedForeground },
    container: { width: "100%" },
    sizeMd: { fontSize: primitives.typography.base },
    sizeSm: { fontSize: primitives.typography.sm },
    sizeXs: { fontSize: primitives.typography.xs },
    text: { fontFamily: typography.body.fontFamily },
  });
};

export const PageNumber = ({
  format = "Page {page} of {total}",
  align = "center",
  size = "sm",
  muted = true,
  style,
}: PageNumberProps) => {
  const theme = usePdfcnTheme();
  const styles = useSafeMemo(() => createPageNumberStyles(theme), [theme]);
  const alignMap = {
    center: styles.alignCenter,
    left: styles.alignLeft,
    right: styles.alignRight,
  } as Record<PageNumberAlign, Style>;
  const sizeMap = {
    md: styles.sizeMd,
    sm: styles.sizeSm,
    xs: styles.sizeXs,
  } as Record<PageNumberSize, Style>;
  const textStyles: Style[] = [
    styles.text,
    alignMap[align],
    sizeMap[size],
    muted ? styles.colorMuted : styles.colorForeground,
  ];
  if (style) {
    textStyles.push(...[style].flat());
  }
  return (
    <View style={styles.container}>
      <Text style={textStyles as never}>
        {format.replace("{page}", "{{pageNumber}}").replace("{total}", "{{totalPages}}")}
      </Text>
    </View>
  );
};
