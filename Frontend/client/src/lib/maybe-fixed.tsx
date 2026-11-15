import type { ReactNode } from "react";

import { Fixed, View } from "@/lib/pdf-primitives";
import type { Style } from "@/lib/pdf-primitives";

export const MaybeFixed = ({
  fixed,
  position,
  wrap,
  style,
  children,
}: {
  fixed?: boolean;
  position: "header" | "footer";
  wrap?: boolean;
  style?: Style | Style[];
  children?: ReactNode;
}) => {
  const view = (
    <View wrap={wrap} style={style as never}>
      {children}
    </View>
  );
  if (!fixed) {
    return view;
  }
  return <Fixed position={position}>{view}</Fixed>;
};
