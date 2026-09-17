import logoWordmark from "../../assets/logo.jpeg";
import logoMark from "../../assets/logo-mark.png";
import styles from "./Logo.module.css";

/**
 * The Raw2Query brand lockup, from the artwork in src/assets.
 *
 * Two files rather than one scaled image: the supplied logo is a wide
 * wordmark (1280x428), which shrinks to an illegible smear in a 72px
 * collapsed rail. `logo-mark.png` is that same artwork cropped square to
 * the document→database mark, so the collapsed nav keeps a recognisable
 * brand without the text.
 */
export function Logo({ collapsed = false }) {
  if (collapsed) {
    return <img className={styles.mark} src={logoMark} alt="Raw2Query" width={44} height={44} />;
  }
  return <img className={styles.wordmark} src={logoWordmark} alt="Raw2Query" />;
}
