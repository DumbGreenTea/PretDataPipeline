import styles from './page.module.css'

/*
 * NOTA: Esta versión NO requiere 'lucide-react' ni 'tailwindcss'.
 * Usa el archivo 'page.module.css' para todos los estilos.
 * Asegúrate de que tu 'layout.tsx' importe 'globals.css'.
 */
export default function Home() {
  return (
    <main className={styles.main}>
      {/* Sección de Navegación */}
      <header className={styles.header}>
        <div className={styles.logo}>
          <span></span>
          Proyecto
        </div>
        <nav className={styles.nav}>
          <a href="#">Inicio</a>
          <a href="#">Servicios</a>
          <a href="#">Contacto</a>
        </nav>
      </header>

      {/* Sección Principal "Hero" */}
      <section className={styles.hero}>
        <h1 className={styles.heroTitle}>Construye Algo Increíble</h1>
        <p className={styles.heroSubtitle}>
          Este es tu nuevo punto de partida. Una plantilla simple y estilizada
          lista para que la personalices y la hagas tuya.
        </p>
        <div className={styles.heroButtons}>
          <a href="#" className={styles.buttonPrimary}>
            Empezar Ahora
          </a>
          <a href="#" className={styles.buttonSecondary}>
            Saber Más
          </a>
        </div>
      </section>

      {/* Sección de Características */}
      <section className={styles.features}>
        <h2 className={styles.sectionTitle}>Características Principales</h2>
        <div className={styles.featuresGrid}>
          <div className={styles.card}>
            <h3>🚀 Despliegue Rápido</h3>
            <p>
              Construido con herramientas modernas para un desarrollo y
              despliegue ágiles.
            </p>
          </div>
          <div className={styles.card}>
            <h3>📱 Diseño Adaptable</h3>
            <p>
              Se ve genial en todos los dispositivos, desde móviles hasta
              computadoras de escritorio.
            </p>
          </div>
          <div className={styles.card}>
            <h3>⚡ Velocidad Extrema</h3>
            <p>
              Optimizado para el rendimiento y ofrecer una gran experiencia de
              usuario.
            </p>
          </div>
        </div>
      </section>

      {/* Pie de Página */}
      <footer className={styles.footer}>
        <p>© 2025 ProjectCo. Todos los derechos reservados.</p>
        <div className={styles.socialLinks}>
          <a href="#">Twitter</a>
          <a href="#">GitHub</a>
          <a href="#">LinkedIn</a>
        </div>
      </footer>
    </main>
  )
}
