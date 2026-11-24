import js from '@eslint/js';
import ts from 'typescript-eslint';
import globals from 'globals';
export default ts.config({ignores:['dist/**','node_modules/**','tests/**']},js.configs.recommended,...ts.configs.recommended,{files:['src/**/*.{ts,tsx}','vite.config.ts'],languageOptions:{globals:globals.browser},rules:{'@typescript-eslint/no-unused-vars':['error',{argsIgnorePattern:'^_'}]}});
