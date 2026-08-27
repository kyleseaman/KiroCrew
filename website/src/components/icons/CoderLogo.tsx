import { BrandGlyph } from '../BrandIcon'
import coderLogoUrl from './coder-shorthand-logo.svg'

const CODER_LOGO_ASPECT_RATIO = 425.93 / 200

/** Official Coder shorthand mark, rendered as a theme-aware CSS mask. */
export default function CoderLogo({ height = 14 }: { height?: number }) {
  return (
    <BrandGlyph
      url={coderLogoUrl}
      size={Math.round(height * CODER_LOGO_ASPECT_RATIO)}
      height={height}
      testId="coder-logo"
    />
  )
}
