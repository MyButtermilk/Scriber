import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "output/**", "src-tauri/**", "client/public/**"],
  },
  {
    files: ["**/*.{js,ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: "latest",
      globals: {
        ...globals.browser,
        ...globals.node,
      },
      parserOptions: {
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactRefresh.configs.vite.rules,
      "no-constant-condition": ["error", { checkLoops: false }],
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
      "react-refresh/only-export-components": "off",
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
    },
  },
  {
    files: ["client/src/App.tsx", "client/src/pages/**/*.tsx", "client/src/components/**/*.tsx"],
    ignores: ["client/src/components/ui/**", "client/src/components/theme-provider.tsx"],
    rules: {
      "react-refresh/only-export-components": [
        "error",
        {
          allowConstantExport: true,
        },
      ],
    },
  },
);
