import java.util.Properties

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
}

// One version for the whole project, from the repository's VERSION file: MAJOR.MINOR.PATCH,
// or MAJOR.MINOR.PATCH-beta.N with N from 1 to 98. The version code puts each beta before
// its release: 0.1.0-beta.1 is 10001 and 0.1.0 is 10099.
val hyadexVersion = providers.fileContents(layout.projectDirectory.file("../../../VERSION")).asText.get().trim()
val hyadexVersionCode = Regex("""(\d{1,3})\.(\d{1,2})\.(\d{1,2})(?:-beta\.([1-9]|[1-8]\d|9[0-8]))?""")
  .matchEntire(hyadexVersion)?.destructured
  ?.let { (major, minor, patch, beta) -> major.toInt() * 1_000_000 + minor.toInt() * 10_000 + patch.toInt() * 100 + (beta.toIntOrNull() ?: 99) }
  ?: error("VERSION should read MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-beta.N, not \"$hyadexVersion\"")

// Release builds are signed with the key that ~/.android/hyadex-release.properties
// names (storeFile, storePassword, keyAlias, keyPassword), or the file HYADEX_SIGNING
// points at. The key stays outside the repository.
val releaseKeyFile = File(System.getenv("HYADEX_SIGNING") ?: "${System.getProperty("user.home")}/.android/hyadex-release.properties")
val releaseKey = releaseKeyFile.takeIf { it.isFile }?.let { file -> Properties().apply { file.inputStream().use { load(it) } } }

android {
  namespace = "dev.hyadex.android"
  compileSdk = 37
  defaultConfig {
    applicationId = "dev.hyadex.android"
    minSdk = 29
    targetSdk = 37
    versionCode = hyadexVersionCode
    versionName = hyadexVersion
  }

  signingConfigs {
    releaseKey?.let { key ->
      create("release") {
        storeFile = releaseKeyFile.resolveSibling(key.getProperty("storeFile"))
        storePassword = key.getProperty("storePassword")
        keyAlias = key.getProperty("keyAlias")
        keyPassword = key.getProperty("keyPassword")
      }
    }
  }

  buildTypes {
    release {
      // R8 drops what the app never calls (most of the icon set, unused library code).
      isMinifyEnabled = true
      isShrinkResources = true
      proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
      // The release key when this machine has one; otherwise the local debug key, so a
      // fresh checkout or CI still builds an installable APK.
      signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
    }
  }
  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  buildFeatures {
    compose = true
    aidl = false
    buildConfig = false
    shaders = false
  }

  packaging {
    resources {
      excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
  }
}

composeCompiler {
  // Immutable timeline models: unchanged rows skip recomposition.
  stabilityConfigurationFiles.add(project.layout.projectDirectory.file("compose-stability.conf"))
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  implementation(libs.androidx.compose.material.icons)

  // Server: REST and event sockets, JSON, images, Markdown
  implementation(libs.okhttp)
  implementation(libs.kotlinx.serialization.json)
  implementation(libs.kotlinx.coroutines.android)
  implementation(libs.coil.compose)
  implementation(libs.coil.network.okhttp)
  implementation(libs.markdown.renderer.m3)
  implementation(libs.markdown.renderer.code)
  // Pairing: scans the code the Mac shows; works without Google Play services.
  implementation(libs.zxing.embedded)
  implementation(libs.zxing.core)
}
