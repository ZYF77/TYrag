/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        sidebar: {
          DEFAULT: '#f7f8fa',
          hover: '#eef0f4',
          active: '#e4e7ed',
        },
      },
    },
  },
  plugins: [],
};
