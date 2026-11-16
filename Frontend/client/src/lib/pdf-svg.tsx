import { Svg as FormeSvg } from "@formepdf/react";
import { Children, Fragment, cloneElement, createElement, isValidElement } from "react";
import type { ReactNode, CSSProperties } from "react";

type AnyProps = Record<string, unknown> & {
  children?: ReactNode;
  style?: CSSProperties;
};

const SVG_ATTRIBUTE_NAMES: Record<string, string> = {
  className: "class",
  dominantBaseline: "dominant-baseline",
  fillOpacity: "fill-opacity",
  fontFamily: "font-family",
  fontSize: "font-size",
  fontStyle: "font-style",
  fontWeight: "font-weight",
  strokeDasharray: "stroke-dasharray",
  strokeLinecap: "stroke-linecap",
  strokeLinejoin: "stroke-linejoin",
  strokeOpacity: "stroke-opacity",
  strokeWidth: "stroke-width",
  textAnchor: "text-anchor",
};

const normalizeSvgProps = (props: AnyProps): AnyProps => {
  const normalized: AnyProps = {};

  for (const [name, value] of Object.entries(props)) {
    if (value === undefined) {
      continue;
    }

    if (name === "style" && value && typeof value === "object") {
      for (const [styleName, styleValue] of Object.entries(value)) {
        normalized[SVG_ATTRIBUTE_NAMES[styleName] ?? styleName] = styleValue;
      }
      continue;
    }

    normalized[SVG_ATTRIBUTE_NAMES[name] ?? name] = value;
  }

  return normalized;
};

const resolveSvgChildren = (children: ReactNode): ReactNode =>
  Children.map(children, (child) => {
    if (!isValidElement<AnyProps>(child)) {
      return child;
    }

    if (typeof child.type === "function") {
      const resolved = (child.type as (props: AnyProps) => ReactNode)(child.props);
      return resolveSvgChildren(resolved);
    }

    if ((child.type as unknown) === Fragment) {
      return resolveSvgChildren(child.props.children);
    }

    if (child.props.children === undefined) {
      return child;
    }

    return cloneElement(child, undefined, resolveSvgChildren(child.props.children));
  });

export const Svg = ({
  children,
  width,
  height,
  viewBox,
  style,
  ...rest
}: AnyProps & { width?: number; height?: number; viewBox?: string }) => (
  <FormeSvg
    width={width as number}
    height={height as number}
    viewBox={viewBox as string | undefined}
    style={style as never}
    {...rest}
  >
    {resolveSvgChildren(children)}
  </FormeSvg>
);

export const Circle = (props: AnyProps) => createElement("circle", normalizeSvgProps(props));
export const Rect = (props: AnyProps) => createElement("rect", normalizeSvgProps(props));
export const G = (props: AnyProps) => createElement("g", normalizeSvgProps(props));
export const Line = (props: AnyProps) => createElement("line", normalizeSvgProps(props));
export const Path = (props: AnyProps) => createElement("path", normalizeSvgProps(props));
export const SvgText = (props: AnyProps) =>
  createElement("text", normalizeSvgProps({ fontFamily: "sans-serif", ...props }));
