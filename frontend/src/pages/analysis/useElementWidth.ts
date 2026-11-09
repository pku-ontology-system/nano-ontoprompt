import { useCallback, useState } from 'react'

/** The width of an element, kept up to date as it is resized. Pass `ref` to the element. */
export function useElementWidth(): [(el: HTMLElement | null) => void | (() => void), number] {
  const [width, setWidth] = useState(0)
  const ref = useCallback((el: HTMLElement | null) => {
    if (!el) return undefined
    const observer = new ResizeObserver(entries => setWidth(Math.round(entries[0].contentRect.width)))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}
