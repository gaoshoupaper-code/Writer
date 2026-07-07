
import { useEffect, useState } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

/** 读取 html[data-theme] 属性，同步 Sonner 的明暗主题 */
function useDataTheme() {
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    const el = document.documentElement;
    const sync = () =>
      setTheme(el.getAttribute("data-theme") === "dark" ? "dark" : "light");

    sync();
    const observer = new MutationObserver(sync);
    observer.observe(el, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  return theme;
}

export function Toaster(props: ToasterProps) {
  const theme = useDataTheme();

  return (
    <Sonner
      theme={theme}
      className="toaster group"
      richColors
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
        },
      }}
      {...props}
    />
  );
}
